import streamlit as st
import pandas as pd
import psycopg
import io
import zipfile
from graphviz import Digraph

# --- OPTIMIZED DB CONNECTION (CACHED) ---
@st.cache_resource
def get_db_connection():
    """Establishes a persistent connection to the database."""
    return psycopg.connect(st.secrets["DB_URI"], autocommit=True)

try:
    conn = get_db_connection()
except Exception as e:
    st.error("❌ Connection failed. Please check your DB_URI in secrets.")
    st.stop()

def run_query(query, params=None):
    """Helper to run queries with error handling for schema changes."""
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            if query.strip().upper().startswith("SELECT"):
                return cur.fetchall(), cur.description
    except psycopg.errors.FeatureNotSupported:
        # Handles the 'cached plan' error by forcing a reload next time
        st.cache_resource.clear()
        st.rerun()
    except Exception as e:
        st.error(f"Database Error: {e}")
    return None, None

def get_df(query, params=None):
    """Helper to get a Pandas DataFrame efficiently."""
    try:
        return pd.read_sql(query, conn, params=params)
    except Exception:
        # If schema changed, read_sql might fail. Clear cache and retry.
        st.cache_resource.clear()
        return pd.DataFrame()

def init_db():
    with conn.cursor() as cur:
        # 1. Projects
        cur.execute("CREATE TABLE IF NOT EXISTS projects (id SERIAL PRIMARY KEY, name TEXT UNIQUE)")
        
        # 2. Switches
        cur.execute("""CREATE TABLE IF NOT EXISTS switches (
            id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id), 
            name TEXT UNIQUE, role TEXT, ip_address TEXT, mac TEXT, 
            clock_source TEXT, jitter_type TEXT, remarks TEXT)""")
        
        # 3. SFPs
        cur.execute("""CREATE TABLE IF NOT EXISTS sfps (
            id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id),
            serial TEXT UNIQUE, wavelength TEXT, channel TEXT, 
            alpha FLOAT DEFAULT 0, delta_tx FLOAT DEFAULT 0, delta_rx FLOAT DEFAULT 0, 
            remarks TEXT)""")
        
        # 4. Ports
        cur.execute("""CREATE TABLE IF NOT EXISTS ports (
            id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id),
            switch_id INTEGER REFERENCES switches(id), port_num INTEGER, 
            sfp_id INTEGER REFERENCES sfps(id),
            remote_sfp_id INTEGER REFERENCES sfps(id),
            connected_to_id INTEGER REFERENCES switches(id), connected_port_num INTEGER,
            port_delta_tx FLOAT DEFAULT 0, port_delta_rx FLOAT DEFAULT 0,
            vlan INTEGER)""")
        
        # Migrations
        cur.execute("ALTER TABLE switches ADD COLUMN IF NOT EXISTS clock_source TEXT")
        cur.execute("ALTER TABLE switches ADD COLUMN IF NOT EXISTS jitter_type TEXT")
        cur.execute("ALTER TABLE switches ADD COLUMN IF NOT EXISTS remarks TEXT")
        cur.execute("ALTER TABLE sfps ADD COLUMN IF NOT EXISTS delta_tx FLOAT DEFAULT 0")
        cur.execute("ALTER TABLE sfps ADD COLUMN IF NOT EXISTS delta_rx FLOAT DEFAULT 0")
        cur.execute("ALTER TABLE sfps ADD COLUMN IF NOT EXISTS remarks TEXT")
        cur.execute("ALTER TABLE ports ADD COLUMN IF NOT EXISTS remote_sfp_id INTEGER")
        cur.execute("ALTER TABLE ports ADD COLUMN IF NOT EXISTS port_delta_tx FLOAT DEFAULT 0")
        cur.execute("ALTER TABLE ports ADD COLUMN IF NOT EXISTS port_delta_rx FLOAT DEFAULT 0")
        cur.execute("ALTER TABLE ports ADD COLUMN IF NOT EXISTS vlan INTEGER")

# --- APP SETUP ---
st.set_page_config(layout="wide", page_title="White Rabbit Manager")
init_db()

# --- SIDEBAR ---
st.sidebar.title("📂 Network Selector")

# 1. Fetch Projects
all_projects = get_df("SELECT * FROM projects")

# 2. Project Selection Logic
selected_project = None
p_id = None

if not all_projects.empty:
    selected_project = st.sidebar.selectbox("Active Network", all_projects['name'])
    p_id = int(all_projects[all_projects['name'] == selected_project]['id'].values[0])
else:
    st.sidebar.warning("⚠️ No networks found. Create one below.")

# 3. Create Project (ALWAYS VISIBLE NOW)
with st.sidebar.expander("➕ Create New Network", expanded=all_projects.empty):
    new_p = st.text_input("Network Name")
    if st.button("Create Network"):
        if new_p:
            run_query("INSERT INTO projects (name) VALUES (%s)", (new_p,))
            st.rerun()

# 4. Stop if no project is selected
if p_id is None:
    st.info("Please create or select a network to continue.")
    st.stop()

st.sidebar.divider()

# 5. Delete Project
with st.sidebar.expander("❌ Delete Project"):
    if st.button("DELETE PROJECT"):
        run_query("DELETE FROM ports WHERE project_id=%s", (p_id,))
        run_query("DELETE FROM sfps WHERE project_id=%s", (p_id,))
        run_query("DELETE FROM switches WHERE project_id=%s", (p_id,))
        run_query("DELETE FROM projects WHERE id=%s", (p_id,))
        st.cache_resource.clear() # Clear cache to refresh state immediately
        st.rerun()

# --- MAIN UI ---
st.title(f"🐇 {selected_project} Dashboard")
tabs = st.tabs(["🗺️ Map", "🖥️ Switches", "🔌 SFPs", "⚙️ Connections", "💾 Backup", "📐 Calc"])

# --- TAB 1: SWITCHES ---
with tabs[1]:
    st.subheader("Switches")
    df_sw = get_df("SELECT * FROM switches WHERE project_id=%s ORDER BY name", (p_id,))
    st.dataframe(df_sw, use_container_width=True)

    with st.form("sw_form"):
        st.write("**Add / Update Switch**")
        c1, c2, c3 = st.columns(3)
        sw_name = c1.text_input("Name", placeholder="e.g. WRS-1")
        sw_ip = c2.text_input("IP")
        sw_mac = c3.text_input("MAC")
        
        c4, c5, c6 = st.columns(3)
        sw_role = c4.selectbox("Role", ["Grandmaster", "Boundary", "Slave", "Timescale Slave"])
        sw_clk = c5.text_input("Clock Source")
        sw_jit = c6.selectbox("Jitter Type", ["Normal", "Low Jitter"]) 
        
        c7 = st.columns(1)[0]
        sw_rem = c7.text_input("Remarks")

        if st.form_submit_button("Save Switch"):
            if sw_name:
                run_query("""INSERT INTO switches (project_id, name, ip_address, mac, role, clock_source, jitter_type, remarks) 
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s) 
                             ON CONFLICT (name) DO UPDATE SET 
                                ip_address=EXCLUDED.ip_address, 
                                mac=EXCLUDED.mac, 
                                role=EXCLUDED.role, 
                                clock_source=EXCLUDED.clock_source,
                                jitter_type=EXCLUDED.jitter_type,
                                remarks=EXCLUDED.remarks""", 
                             (p_id, sw_name, sw_ip, sw_mac, sw_role, sw_clk, sw_jit, sw_rem))
                st.rerun()

    with st.expander("🗑️ Delete Switch"):
        if not df_sw.empty:
            del_sw = st.selectbox("Select Switch", df_sw['name'])
            if st.button("Confirm Delete Switch"):
                sid = int(df_sw[df_sw['name']==del_sw]['id'].values[0])
                run_query("DELETE FROM ports WHERE switch_id=%s OR connected_to_id=%s", (sid, sid))
                run_query("DELETE FROM switches WHERE id=%s", (sid,))
                st.rerun()

# --- TAB 2: SFPs ---
with tabs[2]:
    st.subheader("SFPs")
    df_sfp = get_df("SELECT * FROM sfps WHERE project_id=%s ORDER BY serial", (p_id,))
    st.dataframe(df_sfp, use_container_width=True)

    with st.form("sfp_form"):
        st.write("**Add / Update SFP**")
        c1, c2, c3, c4 = st.columns(4)
        sn = c1.text_input("Serial Number")
        ch = c2.text_input("Channel")
        wv = c3.text_input("Wavelength")
        al = c4.number_input("Alpha", value=0.0, format="%.6f")
        c5, c6, c7 = st.columns(3)
        dtx = c5.number_input("SFP Delta Tx", value=0.0, format="%.4f")
        drx = c6.number_input("SFP Delta Rx", value=0.0, format="%.4f")
        rem = c7.text_input("Remarks")

        if st.form_submit_button("Save SFP"):
            if sn:
                run_query("""INSERT INTO sfps (project_id, serial, channel, wavelength, alpha, delta_tx, delta_rx, remarks) 
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s) 
                             ON CONFLICT (serial) DO UPDATE SET channel=EXCLUDED.channel, wavelength=EXCLUDED.wavelength, alpha=EXCLUDED.alpha, delta_tx=EXCLUDED.delta_tx, delta_rx=EXCLUDED.delta_rx, remarks=EXCLUDED.remarks""", 
                             (p_id, sn, ch, wv, al, dtx, drx, rem))
                st.rerun()

    with st.expander("🗑️ Delete SFP"):
        if not df_sfp.empty:
            del_sfp = st.selectbox("Select SFP", df_sfp['serial'])
            if st.button("Confirm Delete SFP"):
                sid = int(df_sfp[df_sfp['serial']==del_sfp]['id'].values[0])
                run_query("UPDATE ports SET sfp_id=NULL WHERE sfp_id=%s", (sid,))
                run_query("UPDATE ports SET remote_sfp_id=NULL WHERE remote_sfp_id=%s", (sid,))
                run_query("DELETE FROM sfps WHERE id=%s", (sid,))
                st.rerun()

# --- TAB 3: CONNECTIONS ---
with tabs[3]:
    st.subheader("Connections")
    df_p = get_df(f"""
        SELECT p.id, s1.name as local, p.port_num, 
                sfp1.serial as l_sfp,
                s2.name as remote, p.connected_port_num,
                sfp2.serial as r_sfp,
                p.port_delta_tx, p.port_delta_rx, p.vlan
        FROM ports p 
        JOIN switches s1 ON p.switch_id=s1.id 
        LEFT JOIN switches s2 ON p.connected_to_id=s2.id 
        LEFT JOIN sfps sfp1 ON p.sfp_id=sfp1.id
        LEFT JOIN sfps sfp2 ON p.remote_sfp_id=sfp2.id
        WHERE p.project_id=%s ORDER BY s1.name, p.port_num
    """, (p_id,))
    
    st.dataframe(df_p, use_container_width=True)

    mode = st.radio("Action", ["Add New Link", "Edit Existing Link"], horizontal=True)

    sw_opts = df_sw['name'].tolist() if not df_sw.empty else []
    sfp_opts = ["None"] + df_sfp['serial'].tolist() if not df_sfp.empty else ["None"]

    if mode == "Add New Link":
        with st.form("link_form"):
            st.write("**New Connection**")
            c1, c2, c3 = st.columns(3)
            
            l_sw = c1.selectbox("Local Switch", sw_opts)
            l_p = c1.number_input("Local Port", 1, 52)
            l_sfp = c1.selectbox("Local SFP", sfp_opts)
            
            st.write("**Local Port Electronics Calibration**")
            cd1, cd2, cd3 = st.columns(3)
            p_dtx = cd1.number_input("Port Delta Tx (ns)", 0.0, format="%.4f")
            p_drx = cd2.number_input("Port Delta Rx (ns)", 0.0, format="%.4f")
            p_vlan = cd3.number_input("VLAN (0 = None)", 0, 4096, 0)

            st.divider()
            c4, c5, c6 = st.columns(3)
            r_sw = c4.selectbox("Remote Switch", ["None"] + sw_opts)
            r_p = c5.number_input("Remote Port", 1, 52)
            r_sfp = c6.selectbox("Remote SFP", sfp_opts)
            
            if st.form_submit_button("Create Link"):
                if not df_sw.empty and l_sw:
                    lid = int(df_sw[df_sw['name']==l_sw]['id'].values[0])
                    rid = int(df_sw[df_sw['name']==r_sw]['id'].values[0]) if r_sw != "None" else None
                    sid1 = int(df_sfp[df_sfp['serial']==l_sfp]['id'].values[0]) if l_sfp != "None" else None
                    sid2 = int(df_sfp[df_sfp['serial']==r_sfp]['id'].values[0]) if r_sfp != "None" else None
                    
                    vlan_val = int(p_vlan) if p_vlan > 0 else None
                    
                    run_query("""INSERT INTO ports 
                        (project_id, switch_id, port_num, sfp_id, remote_sfp_id, connected_to_id, connected_port_num, port_delta_tx, port_delta_rx, vlan) 
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", 
                        (p_id, lid, l_p, sid1, sid2, rid, r_p, p_dtx, p_drx, vlan_val))
                    st.rerun()

    elif mode == "Edit Existing Link":
        if not df_p.empty:
            lbls = df_p.apply(lambda x: f"ID {x['id']}: {x['local']} P{x['port_num']} -> {x['remote']}", axis=1)
            sel = st.selectbox("Select Link", lbls)
            sel_id = int(sel.split(":")[0].replace("ID ", ""))
            
            # Safe access to VLAN
            if not df_p[df_p['id'] == sel_id].empty:
                current_vlan = df_p[df_p['id'] == sel_id]['vlan'].values[0]
                current_vlan = int(current_vlan) if pd.notna(current_vlan) else 0
            else:
                current_vlan = 0

            with st.form("edit_link"):
                st.write(f"Editing {sel}")
                ce1, ce2, ce3 = st.columns(3)
                n_p_dtx = ce1.number_input("Update Port Delta Tx", 0.0, format="%.4f")
                n_p_drx = ce2.number_input("Update Port Delta Rx", 0.0, format="%.4f")
                n_vlan = ce3.number_input("Update VLAN", 0, 4096, current_vlan)
                
                n_lsfp = st.selectbox("Update Local SFP", sfp_opts)
                
                if st.form_submit_button("Update Link"):
                    sid1 = int(df_sfp[df_sfp['serial']==n_lsfp]['id'].values[0]) if n_lsfp != "None" else None
                    vlan_val = int(n_vlan) if n_vlan > 0 else None
                    
                    run_query("UPDATE ports SET port_delta_tx=%s, port_delta_rx=%s, vlan=%s, sfp_id=%s WHERE id=%s", 
                                 (n_p_dtx, n_p_drx, vlan_val, sid1, sel_id))
                    st.rerun()

    with st.expander("🗑️ Delete Link"):
        if not df_p.empty:
            d_lbl = st.selectbox("Remove Link", df_p.apply(lambda x: f"ID {x['id']}: {x['local']} P{x['port_num']}", axis=1))
            if st.button("Delete Selected Link"):
                lid_del = int(d_lbl.split(":")[0].replace("ID ", ""))
                run_query("DELETE FROM ports WHERE id=%s", (lid_del,))
                st.rerun()

# --- TAB 4: BACKUP ---
with tabs[4]:
    st.subheader("Backup")
    if st.button("📦 Generate ZIP"):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("switches.csv", get_df("SELECT * FROM switches WHERE project_id=%s", (p_id,)).to_csv(index=False))
            zf.writestr("sfps.csv", get_df("SELECT * FROM sfps WHERE project_id=%s", (p_id,)).to_csv(index=False))
            zf.writestr("ports.csv", get_df("SELECT * FROM ports WHERE project_id=%s", (p_id,)).to_csv(index=False))
        st.download_button("⬇️ Download", zip_buffer.getvalue(), f"WR_Backup.zip", "application/zip")

# --- TAB 0: MAP ---
with tabs[0]:
    links = get_df("SELECT switch_id, connected_to_id, port_num, connected_port_num, vlan FROM ports WHERE project_id=%s AND connected_to_id IS NOT NULL", (p_id,))
    if not df_sw.empty:
        dot = Digraph(format='pdf')
        dot.attr(rankdir='LR')
        for _, s in df_sw.iterrows():
            jit_lbl = f" ({s['jitter_type']})" if s['jitter_type'] else ""
            dot.node(str(s['id']), f"{s['name']}{jit_lbl}\n{s['role']}\n{s['ip_address']}")
        for _, l in links.iterrows():
            vlan_txt = f"\nVLAN: {int(l['vlan'])}" if pd.notna(l['vlan']) else ""
            dot.edge(str(l['switch_id']), str(l['connected_to_id']), label=f"P{l['port_num']}:P{l['connected_port_num']}{vlan_txt}")
        st.graphviz_chart(dot)
        try: st.download_button("📥 PDF", dot.pipe(), "topology.pdf")
        except: pass

# --- TAB 5: CALC ---
with tabs[5]:
    st.subheader("Fiber Calc")
    d = st.number_input("Km", 0.0)
    st.metric("Delay", f"{(d * 1000 * 1.4682 / 299792458 * 1e9):.2f} ns")
