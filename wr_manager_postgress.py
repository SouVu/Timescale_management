import streamlit as st
import pandas as pd
import psycopg
import io
import zipfile
from graphviz import Digraph

# --- 1. DB CONNECTION ---
@st.cache_resource
def get_db_connection():
    try:
        return psycopg.connect(st.secrets["DB_URI"], autocommit=True)
    except Exception as e:
        st.error(f"❌ Connection failed: {e}")
        st.stop()

conn = get_db_connection()

# --- 2. HELPERS ---
def run_query(query, params=None, fetch=False):
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            if fetch:
                return cur.fetchall(), cur.description
            return True, None
    except Exception as e:
        st.error(f"SQL Error: {e}")
        return False, None

def get_df(query, params=None):
    try:
        return pd.read_sql(query, conn, params=params)
    except Exception:
        st.cache_resource.clear()
        return pd.DataFrame()

def init_db():
    with conn.cursor() as cur:
        # Projects
        cur.execute("""CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY, name TEXT UNIQUE
        )""")
        
        # Switches (Added jitter_mode)
        cur.execute("""CREATE TABLE IF NOT EXISTS switches (
            id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE, 
            name TEXT, role TEXT, ip_address TEXT, mac TEXT, clock_source TEXT,
            jitter_mode TEXT DEFAULT 'Normal',
            UNIQUE(project_id, name)
        )""")
        
        # SFPs
        cur.execute("""CREATE TABLE IF NOT EXISTS sfps (
            id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            serial TEXT, wavelength TEXT, channel TEXT, alpha FLOAT DEFAULT 0, 
            delta_tx FLOAT DEFAULT 0, delta_rx FLOAT DEFAULT 0,
            UNIQUE(project_id, serial)
        )""")
        
        # Ports
        cur.execute("""CREATE TABLE IF NOT EXISTS ports (
            id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            switch_id INTEGER REFERENCES switches(id) ON DELETE CASCADE, 
            port_num INTEGER, sfp_id INTEGER REFERENCES sfps(id) ON DELETE SET NULL,
            remote_sfp_id INTEGER REFERENCES sfps(id) ON DELETE SET NULL,
            connected_to_id INTEGER REFERENCES switches(id) ON DELETE SET NULL, 
            connected_port_num INTEGER, port_delta_tx FLOAT DEFAULT 0, 
            port_delta_rx FLOAT DEFAULT 0, vlan INTEGER
        )""")

        # MIGRATION: Ensure jitter_mode exists in old DBs
        cur.execute("ALTER TABLE switches ADD COLUMN IF NOT EXISTS jitter_mode TEXT DEFAULT 'Normal'")

# --- 3. DUPLICATE LOGIC ---
def duplicate_network(old_pid, new_name):
    try:
        with conn.cursor() as cur:
            # Copy Project
            cur.execute("INSERT INTO projects (name) VALUES (%s) RETURNING id", (new_name,))
            new_pid = cur.fetchone()[0]
            
            # Copy Switches
            cur.execute("SELECT id, name, role, ip_address, mac, clock_source, jitter_mode FROM switches WHERE project_id=%s", (old_pid,))
            switches = cur.fetchall()
            sw_map = {} 
            for s in switches:
                cur.execute("INSERT INTO switches (project_id, name, role, ip_address, mac, clock_source, jitter_mode) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id", 
                               (new_pid, s[1], s[2], s[3], s[4], s[5], s[6]))
                sw_map[s[0]] = cur.fetchone()[0]

            # Copy SFPs
            cur.execute("SELECT id, serial, wavelength, channel, alpha, delta_tx, delta_rx FROM sfps WHERE project_id=%s", (old_pid,))
            sfps = cur.fetchall()
            sfp_map = {}
            for s in sfps:
                cur.execute("INSERT INTO sfps (project_id, serial, wavelength, channel, alpha, delta_tx, delta_rx) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id", 
                               (new_pid, s[1], s[2], s[3], s[4], s[5], s[6]))
                sfp_map[s[0]] = cur.fetchone()[0]

            # Copy Ports
            cur.execute("SELECT switch_id, port_num, sfp_id, remote_sfp_id, connected_to_id, connected_port_num, port_delta_tx, port_delta_rx, vlan FROM ports WHERE project_id=%s", (old_pid,))
            ports = cur.fetchall()
            for p in ports:
                sid = sw_map.get(p[0])
                cid = sw_map.get(p[4])
                sfpid = sfp_map.get(p[2])
                rsfpid = sfp_map.get(p[3])
                if sid:
                    cur.execute("INSERT INTO ports (project_id, switch_id, port_num, sfp_id, remote_sfp_id, connected_to_id, connected_port_num, port_delta_tx, port_delta_rx, vlan) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", 
                                   (new_pid, sid, p[1], sfpid, rsfpid, cid, p[5], p[6], p[7], p[8]))
        return True
    except Exception as e:
        st.error(f"Clone Failed: {e}")
        return False

# --- 4. APP START ---
st.set_page_config(layout="wide", page_title="WR Manager Stable")
init_db()

# --- SIDEBAR ---
st.sidebar.title("🎛️ Network Manager")

# 1. Project Selector
projects = get_df("SELECT * FROM projects ORDER BY id")
pid = None
selected_project = None

if not projects.empty:
    selected_project = st.sidebar.selectbox("Active Network", projects['name'])
    pid = int(projects[projects['name'] == selected_project]['id'].values[0])
else:
    st.sidebar.warning("No networks found.")

# 2. Create Project (Always Visible)
with st.sidebar.expander("➕ Create New Network", expanded=projects.empty):
    new_p_name = st.text_input("Name")
    if st.button("Create"):
        if new_p_name:
            if run_query("INSERT INTO projects (name) VALUES (%s)", (new_p_name,))[0]:
                st.rerun()
            else:
                st.error("Name taken.")

# STOP IF NO PROJECT
if pid is None:
    st.stop()

# 3. Duplicate
st.sidebar.divider()
with st.sidebar.expander("⚡ Duplicate Network"):
    clone_name = st.text_input("New Name")
    if st.button("Duplicate Now"):
        if duplicate_network(pid, clone_name):
            st.success("Done!")
            st.rerun()

# 4. DELETE
st.sidebar.subheader("Danger Zone")
if st.sidebar.button("🗑️ DELETE CURRENT NETWORK", type="primary"):
    run_query("DELETE FROM projects WHERE id=%s", (pid,))
    st.rerun()

# --- MAIN TABS ---
st.title(f"🐇 {selected_project}")
tabs = st.tabs(["🗺️ Map", "🖥️ Switches", "🔌 SFPs", "⚙️ Connections", "💾 Backup", "📐 Calc"])

# --- TAB 1: SWITCHES ---
with tabs[1]:
    st.subheader("Switches")
    sw_df = get_df("SELECT * FROM switches WHERE project_id=%s ORDER BY name", (pid,))
    st.dataframe(sw_df.drop(columns=['project_id']), use_container_width=True)

    # Add/Edit Form
    with st.form("sw_form"):
        st.write("### Add / Edit Switch")
        c1, c2, c3 = st.columns(3)
        s_name = c1.text_input("Name (Unique)", placeholder="WRS-1")
        s_ip = c2.text_input("IP Address")
        s_mac = c3.text_input("MAC Address")
        
        c4, c5, c6 = st.columns(3)
        s_role = c4.selectbox("Role", ["Grandmaster", "Boundary", "Slave"])
        s_clk = c5.text_input("Clock Source")
        # RESTORED FEATURE: Jitter Mode
        s_jitter = c6.selectbox("Jitter Support", ["Normal", "Low Jitter"])

        if st.form_submit_button("Save Switch"):
            if s_name:
                exists = not sw_df[sw_df['name'] == s_name].empty
                if exists:
                    run_query("UPDATE switches SET ip_address=%s, mac=%s, role=%s, clock_source=%s, jitter_mode=%s WHERE project_id=%s AND name=%s", 
                                (s_ip, s_mac, s_role, s_clk, s_jitter, pid, s_name))
                    st.success("Updated!")
                else:
                    if run_query("INSERT INTO switches (project_id, name, ip_address, mac, role, clock_source, jitter_mode) VALUES (%s, %s, %s, %s, %s, %s, %s)", 
                                (pid, s_name, s_ip, s_mac, s_role, s_clk, s_jitter))[0]:
                        st.success("Created!")
                    else:
                        st.error("Name duplicate.")
                st.rerun()

    if not sw_df.empty:
        with st.expander("Delete Switch"):
            del_s = st.selectbox("Select", sw_df['name'])
            if st.button("Delete Switch"):
                run_query("DELETE FROM switches WHERE project_id=%s AND name=%s", (pid, del_s))
                st.rerun()

# --- TAB 2: SFPs ---
with tabs[2]:
    st.subheader("SFPs")
    sfp_df = get_df("SELECT * FROM sfps WHERE project_id=%s ORDER BY serial", (pid,))
    st.dataframe(sfp_df.drop(columns=['project_id']), use_container_width=True)

    with st.form("sfp_form"):
        st.write("### Add / Edit SFP")
        c1, c2, c3, c4 = st.columns(4)
        sn = c1.text_input("Serial")
        ch = c2.text_input("Channel")
        wv = c3.text_input("Wavelength")
        al = c4.number_input("Alpha", format="%.6f")
        c5, c6 = st.columns(2)
        dtx = c5.number_input("Delta Tx", format="%.4f")
        drx = c6.number_input("Delta Rx", format="%.4f")

        if st.form_submit_button("Save SFP"):
            if sn:
                exists = not sfp_df[sfp_df['serial'] == sn].empty
                if exists:
                    run_query("UPDATE sfps SET channel=%s, wavelength=%s, alpha=%s, delta_tx=%s, delta_rx=%s WHERE project_id=%s AND serial=%s",
                              (ch, wv, al, dtx, drx, pid, sn))
                    st.success("Updated")
                else:
                    run_query("INSERT INTO sfps (project_id, serial, channel, wavelength, alpha, delta_tx, delta_rx) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                              (pid, sn, ch, wv, al, dtx, drx))
                    st.success("Created")
                st.rerun()
    
    if not sfp_df.empty:
        with st.expander("Delete SFP"):
            del_s = st.selectbox("Select SFP", sfp_df['serial'])
            if st.button("Delete SFP"):
                run_query("DELETE FROM sfps WHERE project_id=%s AND serial=%s", (pid, del_s))
                st.rerun()

# --- TAB 3: CONNECTIONS ---
with tabs[3]:
    st.subheader("Connections")
    conn_df = get_df(f"""
        SELECT p.id, s1.name as local, p.port_num, sfp1.serial as l_sfp,
               s2.name as remote, p.connected_port_num, sfp2.serial as r_sfp,
               p.port_delta_tx, p.port_delta_rx, p.vlan
        FROM ports p 
        JOIN switches s1 ON p.switch_id=s1.id 
        LEFT JOIN switches s2 ON p.connected_to_id=s2.id 
        LEFT JOIN sfps sfp1 ON p.sfp_id=sfp1.id
        LEFT JOIN sfps sfp2 ON p.remote_sfp_id=sfp2.id
        WHERE p.project_id=%s ORDER BY s1.name, p.port_num
    """, (pid,))
    st.dataframe(conn_df, use_container_width=True)

    mode = st.radio("Action", ["Add Link", "Edit Link"], horizontal=True)
    sw_opts = sw_df['name'].tolist() if not sw_df.empty else []
    sfp_opts = ["None"] + sfp_df['serial'].tolist() if not sfp_df.empty else ["None"]

    if mode == "Add Link":
        with st.form("lnk_form"):
            c1, c2, c3 = st.columns(3)
            l_sw = c1.selectbox("Local Switch", sw_opts)
            l_p = c1.number_input("Local Port", 1, 52)
            l_sfp = c1.selectbox("Local SFP", sfp_opts)
            
            c4, c5, c6 = st.columns(3)
            p_dtx = c4.number_input("Delta Tx", 0.0)
            p_drx = c5.number_input("Delta Rx", 0.0)
            vlan = c6.number_input("VLAN", 0)

            st.divider()
            r_sw = st.selectbox("Remote Switch", ["None"] + sw_opts)
            r_p = st.number_input("Remote Port", 1, 52)
            r_sfp = st.selectbox("Remote SFP", sfp_opts)

            if st.form_submit_button("Create Link"):
                if l_sw:
                    lid = int(sw_df[sw_df['name']==l_sw]['id'].values[0])
                    rid = int(sw_df[sw_df['name']==r_sw]['id'].values[0]) if r_sw != "None" else None
                    sid1 = int(sfp_df[sfp_df['serial']==l_sfp]['id'].values[0]) if l_sfp != "None" else None
                    sid2 = int(sfp_df[sfp_df['serial']==r_sfp]['id'].values[0]) if r_sfp != "None" else None
                    
                    run_query("INSERT INTO ports (project_id, switch_id, port_num, sfp_id, remote_sfp_id, connected_to_id, connected_port_num, port_delta_tx, port_delta_rx, vlan) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                              (pid, lid, l_p, sid1, sid2, rid, r_p, p_dtx, p_drx, vlan if vlan > 0 else None))
                    st.success("Link Created")
                    st.rerun()

    elif mode == "Edit Link" and not conn_df.empty:
        sel_lbl = st.selectbox("Select Link", conn_df.apply(lambda x: f"ID {x['id']}: {x['local']} P{x['port_num']} -> {x['remote']}", axis=1))
        sel_id = int(sel_lbl.split(":")[0].replace("ID ", ""))
        with st.form("edit_lnk"):
            ndtx = st.number_input("Update Delta Tx", 0.0)
            ndrx = st.number_input("Update Delta Rx", 0.0)
            if st.form_submit_button("Update"):
                run_query("UPDATE ports SET port_delta_tx=%s, port_delta_rx=%s WHERE id=%s", (ndtx, ndrx, sel_id))
                st.rerun()
        if st.button("Delete Link"):
            run_query("DELETE FROM ports WHERE id=%s", (sel_id,))
            st.rerun()

# --- TAB 0: MAP ---
with tabs[0]:
    if not sw_df.empty:
        dot = Digraph(format='pdf')
        dot.attr(rankdir='LR')
        for _, s in sw_df.iterrows():
            dot.node(str(s['id']), f"{s['name']}\n{s['role']}\n{s.get('jitter_mode', 'Normal')}")
        links = get_df("SELECT switch_id, connected_to_id, port_num, connected_port_num FROM ports WHERE project_id=%s AND connected_to_id IS NOT NULL", (pid,))
        for _, l in links.iterrows():
            dot.edge(str(l['switch_id']), str(l['connected_to_id']), label=f"P{l['port_num']}:P{l['connected_port_num']}")
        st.graphviz_chart(dot)

# --- TAB 4: BACKUP ---
with tabs[4]:
    if st.button("📦 Backup ZIP"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("switches.json", sw_df.to_json(orient='records'))
            zf.writestr("sfps.json", sfp_df.to_json(orient='records'))
            zf.writestr("links.json", conn_df.to_json(orient='records'))
        st.download_button("Download", buf.getvalue(), "backup.zip", "application/zip")

# --- TAB 5: CALC ---
with tabs[5]:
    st.subheader("Fiber Calc")
    km = st.number_input("Km", 0.0)
    st.metric("Delay", f"{(km * 1000 * 1.4682 / 299792458 * 1e9):.2f} ns")
