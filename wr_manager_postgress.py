import streamlit as st
import pandas as pd
import psycopg
import io
import zipfile
import json
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
        # Tables
        cur.execute("""CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY, name TEXT UNIQUE, custom_schema JSONB DEFAULT '{}'::jsonb
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS switches (
            id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE, 
            name TEXT, role TEXT, ip_address TEXT, mac TEXT, clock_source TEXT,
            metadata JSONB DEFAULT '{}'::jsonb, UNIQUE(project_id, name)
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS sfps (
            id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            serial TEXT, wavelength TEXT, channel TEXT, alpha FLOAT DEFAULT 0, 
            delta_tx FLOAT DEFAULT 0, delta_rx FLOAT DEFAULT 0,
            metadata JSONB DEFAULT '{}'::jsonb, UNIQUE(project_id, serial)
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS ports (
            id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            switch_id INTEGER REFERENCES switches(id) ON DELETE CASCADE, 
            port_num INTEGER, sfp_id INTEGER REFERENCES sfps(id) ON DELETE SET NULL,
            remote_sfp_id INTEGER REFERENCES sfps(id) ON DELETE SET NULL,
            connected_to_id INTEGER REFERENCES switches(id) ON DELETE SET NULL, 
            connected_port_num INTEGER, port_delta_tx FLOAT DEFAULT 0, 
            port_delta_rx FLOAT DEFAULT 0, vlan INTEGER,
            metadata JSONB DEFAULT '{}'::jsonb
        )""")
        # Migrations
        cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS custom_schema JSONB DEFAULT '{}'::jsonb")
        cur.execute("ALTER TABLE switches ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb")
        cur.execute("ALTER TABLE sfps ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb")
        cur.execute("ALTER TABLE ports ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb")

# --- 3. LOGIC ---
def duplicate_network(old_pid, new_name):
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO projects (name, custom_schema) SELECT %s, custom_schema FROM projects WHERE id=%s RETURNING id", (new_name, old_pid))
            new_pid = cur.fetchone()[0]
            
            # Copy Switches
            cur.execute("SELECT id, name, role, ip_address, mac, clock_source, metadata FROM switches WHERE project_id=%s", (old_pid,))
            switches = cur.fetchall()
            sw_map = {} 
            for s in switches:
                cur.execute("INSERT INTO switches (project_id, name, role, ip_address, mac, clock_source, metadata) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id", 
                               (new_pid, s[1], s[2], s[3], s[4], s[5], s[6]))
                sw_map[s[0]] = cur.fetchone()[0]

            # Copy SFPs
            cur.execute("SELECT id, serial, wavelength, channel, alpha, delta_tx, delta_rx, metadata FROM sfps WHERE project_id=%s", (old_pid,))
            sfps = cur.fetchall()
            sfp_map = {}
            for s in sfps:
                cur.execute("INSERT INTO sfps (project_id, serial, wavelength, channel, alpha, delta_tx, delta_rx, metadata) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id", 
                               (new_pid, s[1], s[2], s[3], s[4], s[5], s[6], s[7]))
                sfp_map[s[0]] = cur.fetchone()[0]

            # Copy Ports
            cur.execute("SELECT switch_id, port_num, sfp_id, remote_sfp_id, connected_to_id, connected_port_num, port_delta_tx, port_delta_rx, vlan, metadata FROM ports WHERE project_id=%s", (old_pid,))
            ports = cur.fetchall()
            for p in ports:
                sid = sw_map.get(p[0])
                cid = sw_map.get(p[4])
                sfpid = sfp_map.get(p[2])
                rsfpid = sfp_map.get(p[3])
                if sid:
                    cur.execute("INSERT INTO ports (project_id, switch_id, port_num, sfp_id, remote_sfp_id, connected_to_id, connected_port_num, port_delta_tx, port_delta_rx, vlan, metadata) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", 
                                   (new_pid, sid, p[1], sfpid, rsfpid, cid, p[5], p[6], p[7], p[8], p[9]))
        return True
    except Exception as e:
        st.error(f"Clone Failed: {e}")
        return False

# --- 4. APP START ---
st.set_page_config(layout="wide", page_title="WR Manager Final")
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

# IF NO PROJECT SELECTED, STOP HERE
if pid is None:
    st.stop()

# 3. Custom Fields
st.sidebar.divider()
with st.sidebar.expander("🛠️ Custom Fields"):
    # Load Schema Safely
    raw_schema = projects[projects['id']==pid]['custom_schema'].values[0]
    current_schema = json.loads(raw_schema) if isinstance(raw_schema, str) else (raw_schema if raw_schema else {})

    # Add Field
    st.write("**Add Field**")
    c1, c2 = st.columns(2)
    c_type = c1.selectbox("Target", ["Switch", "SFP", "Port"])
    c_name = c2.text_input("Field Name")
    if st.button("Add"):
        key = c_type.lower()
        if key not in current_schema: current_schema[key] = []
        if c_name and c_name not in current_schema[key]:
            current_schema[key].append(c_name)
            run_query("UPDATE projects SET custom_schema = %s WHERE id=%s", (json.dumps(current_schema), pid))
            st.rerun()
    
    # Remove Field
    st.write("**Remove Field**")
    r_type = st.selectbox("From", ["Switch", "SFP", "Port"], key="rm_t")
    r_key = r_type.lower()
    if r_key in current_schema and current_schema[r_key]:
        r_field = st.selectbox("Select", current_schema[r_key])
        if st.button("Remove"):
            current_schema[r_key].remove(r_field)
            run_query("UPDATE projects SET custom_schema = %s WHERE id=%s", (json.dumps(current_schema), pid))
            st.rerun()

# 4. Duplicate
st.sidebar.divider()
with st.sidebar.expander("⚡ Duplicate Network"):
    clone_name = st.text_input("New Name")
    if st.button("Duplicate Now"):
        if duplicate_network(pid, clone_name):
            st.success("Done!")
            st.rerun()

# 5. DELETE (VISIBLE NOW)
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
    
    # Show Data
    disp = sw_df.copy()
    if not disp.empty and 'metadata' in disp.columns:
        meta = pd.json_normalize(disp['metadata'])
        disp = disp.drop(columns=['metadata']).join(meta)
    st.dataframe(disp, use_container_width=True)

    # Add/Edit Form
    with st.form("sw_form"):
        st.write("### Add / Edit Switch")
        c1, c2, c3 = st.columns(3)
        s_name = c1.text_input("Name (Unique)", placeholder="WRS-1")
        s_ip = c2.text_input("IP")
        s_mac = c3.text_input("MAC")
        c4, c5 = st.columns(2)
        s_role = c4.selectbox("Role", ["Grandmaster", "Boundary", "Slave"])
        s_clk = c5.text_input("Clock Source")

        # Custom Fields
        meta_vals = {}
        if "switch" in current_schema and current_schema["switch"]:
            st.write("#### Custom Fields")
            cols = st.columns(3)
            for i, f in enumerate(current_schema["switch"]):
                meta_vals[f] = cols[i%3].text_input(f)

        if st.form_submit_button("Save Switch"):
            if s_name:
                meta_json = json.dumps(meta_vals)
                exists = not sw_df[sw_df['name'] == s_name].empty
                if exists:
                    run_query("UPDATE switches SET ip_address=%s, mac=%s, role=%s, clock_source=%s, metadata=%s WHERE project_id=%s AND name=%s", 
                                (s_ip, s_mac, s_role, s_clk, meta_json, pid, s_name))
                    st.success("Updated!")
                else:
                    if run_query("INSERT INTO switches (project_id, name, ip_address, mac, role, clock_source, metadata) VALUES (%s, %s, %s, %s, %s, %s, %s)", 
                                (pid, s_name, s_ip, s_mac, s_role, s_clk, meta_json))[0]:
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
    
    disp_sfp = sfp_df.copy()
    if not disp_sfp.empty and 'metadata' in disp_sfp.columns:
        m_sfp = pd.json_normalize(disp_sfp['metadata'])
        disp_sfp = disp_sfp.drop(columns=['metadata']).join(m_sfp)
    st.dataframe(disp_sfp, use_container_width=True)

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

        meta_sfp = {}
        if "sfp" in current_schema and current_schema["sfp"]:
            st.write("#### Custom Fields")
            cols = st.columns(3)
            for i, f in enumerate(current_schema["sfp"]):
                meta_sfp[f] = cols[i%3].text_input(f)

        if st.form_submit_button("Save SFP"):
            if sn:
                meta_json = json.dumps(meta_sfp)
                exists = not sfp_df[sfp_df['serial'] == sn].empty
                if exists:
                    run_query("UPDATE sfps SET channel=%s, wavelength=%s, alpha=%s, delta_tx=%s, delta_rx=%s, metadata=%s WHERE project_id=%s AND serial=%s",
                              (ch, wv, al, dtx, drx, meta_json, pid, sn))
                    st.success("Updated")
                else:
                    run_query("INSERT INTO sfps (project_id, serial, channel, wavelength, alpha, delta_tx, delta_rx, metadata) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                              (pid, sn, ch, wv, al, dtx, drx, meta_json))
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
               p.port_delta_tx, p.port_delta_rx, p.vlan, p.metadata
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
            
            meta_port = {}
            if "port" in current_schema and current_schema["port"]:
                cols = st.columns(3)
                for i, f in enumerate(current_schema["port"]):
                    meta_port[f] = cols[i%3].text_input(f)

            if st.form_submit_button("Create Link"):
                if l_sw:
                    lid = int(sw_df[sw_df['name']==l_sw]['id'].values[0])
                    rid = int(sw_df[sw_df['name']==r_sw]['id'].values[0]) if r_sw != "None" else None
                    sid1 = int(sfp_df[sfp_df['serial']==l_sfp]['id'].values[0]) if l_sfp != "None" else None
                    sid2 = int(sfp_df[sfp_df['serial']==r_sfp]['id'].values[0]) if r_sfp != "None" else None
                    
                    run_query("INSERT INTO ports (project_id, switch_id, port_num, sfp_id, remote_sfp_id, connected_to_id, connected_port_num, port_delta_tx, port_delta_rx, vlan, metadata) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                              (pid, lid, l_p, sid1, sid2, rid, r_p, p_dtx, p_drx, vlan if vlan > 0 else None, json.dumps(meta_port)))
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
            dot.node(str(s['id']), f"{s['name']}\n{s['role']}")
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
