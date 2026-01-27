import streamlit as st
import pandas as pd
import psycopg
from graphviz import Digraph

# --- DB CONNECTION ---
def get_conn():
    return psycopg.connect(st.secrets["DB_URI"])

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 1. Projects/Networks
            cur.execute("CREATE TABLE IF NOT EXISTS projects (id SERIAL PRIMARY KEY, name TEXT UNIQUE, remarks TEXT)")
            # 2. Switches
            cur.execute("""CREATE TABLE IF NOT EXISTS switches (
                id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id), 
                name TEXT, role TEXT, ip_address TEXT, mac TEXT, clock_source TEXT, remarks TEXT)""")
            # 3. SFPs
            cur.execute("""CREATE TABLE IF NOT EXISTS sfps (
                id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id),
                serial TEXT UNIQUE, wavelength TEXT, alpha FLOAT, delta_tx FLOAT, delta_rx FLOAT)""")
            # 4. Ports
            cur.execute("""CREATE TABLE IF NOT EXISTS ports (
                id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id),
                switch_id INTEGER REFERENCES switches(id), port_num INTEGER, sfp_id INTEGER REFERENCES sfps(id),
                connected_to_id INTEGER REFERENCES switches(id), connected_port_num INTEGER,
                port_delta_tx FLOAT DEFAULT 0, port_delta_rx FLOAT DEFAULT 0, remarks TEXT)""")
            
            # Migrations for existing DBs
            cur.execute("ALTER TABLE switches ADD COLUMN IF NOT EXISTS ip_address TEXT")
            cur.execute("ALTER TABLE switches ADD COLUMN IF NOT EXISTS project_id INTEGER REFERENCES projects(id)")
            cur.execute("ALTER TABLE sfps ADD COLUMN IF NOT EXISTS project_id INTEGER REFERENCES projects(id)")
            cur.execute("ALTER TABLE ports ADD COLUMN IF NOT EXISTS project_id INTEGER REFERENCES projects(id)")
            cur.execute("ALTER TABLE ports ADD COLUMN IF NOT EXISTS connected_port_num INTEGER")
        conn.commit()

# --- APP SETUP ---
st.set_page_config(layout="wide", page_title="White Rabbit NMS")
init_db()

# --- SIDEBAR: PROJECT & CLONE LOGIC ---
st.sidebar.title("📂 Network Management")
with get_conn() as conn:
    all_projects = pd.read_sql("SELECT * FROM projects", conn)

if all_projects.empty:
    st.sidebar.warning("No networks found.")
    new_p_name = st.sidebar.text_input("Create First Network Name")
    if st.sidebar.button("Initialize"):
        with get_conn() as conn: conn.execute("INSERT INTO projects (name) VALUES (%s)", (new_p_name,))
        st.rerun()
    st.stop()

selected_project_name = st.sidebar.selectbox("Active Network", all_projects['name'])
p_id = int(all_projects[all_projects['name'] == selected_project_name]['id'].values[0])

# --- CLONE FEATURE ---
st.sidebar.divider()
st.sidebar.subheader("👯 Clone This Network")
clone_name = st.sidebar.text_input("New Network Name", placeholder="e.g. Uni-Lab-B-Copy")
if st.sidebar.button("🚀 Clone Everything"):
    if clone_name:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # 1. Create New Project
                cur.execute("INSERT INTO projects (name) VALUES (%s) RETURNING id", (clone_name,))
                new_p_id = cur.fetchone()[0]
                
                # 2. Clone SFPs
                cur.execute("INSERT INTO sfps (project_id, serial, wavelength, alpha, delta_tx, delta_rx) SELECT %s, serial || '-' || %s, wavelength, alpha, delta_tx, delta_rx FROM sfps WHERE project_id = %s", (new_p_id, clone_name, p_id))
                
                # 3. Clone Switches & Map IDs
                cur.execute("SELECT id, name, role, ip_address, mac FROM switches WHERE project_id = %s", (p_id,))
                old_switches = cur.fetchall()
                sw_map = {}
                for old_id, name, role, ip, mac in old_switches:
                    cur.execute("INSERT INTO switches (project_id, name, role, ip_address, mac) VALUES (%s,%s,%s,%s,%s) RETURNING id", (new_p_id, name, role, ip, mac))
                    sw_map[old_id] = cur.fetchone()[0]
                
                # 4. Clone Ports using ID Mapping
                cur.execute("SELECT switch_id, port_num, connected_to_id, connected_port_num, port_delta_tx, port_delta_rx FROM ports WHERE project_id = %s", (p_id,))
                for sid, pnum, cid, cpnum, pdtx, pdrx in cur.fetchall():
                    new_sid = sw_map.get(sid)
                    new_cid = sw_map.get(cid) if cid else None
                    cur.execute("INSERT INTO ports (project_id, switch_id, port_num, connected_to_id, connected_port_num, port_delta_tx, port_delta_rx) VALUES (%s,%s,%s,%s,%s,%s,%s)", (new_p_id, new_sid, pnum, new_cid, cpnum, pdtx, pdrx))
            conn.commit()
            st.sidebar.success(f"Cloned into {clone_name}!")
            st.rerun()

st.title(f"🐇 {selected_project_name}")
tabs = st.tabs(["🗺️ Map", "🖥️ Switches", "🔌 SFPs", "⚙️ Ports", "📄 Config"])

# --- TAB: SWITCHES (With Edit/IP) ---
with tabs[1]:
    with get_conn() as conn:
        sw_data = pd.read_sql(f"SELECT * FROM switches WHERE project_id={p_id}", conn)
    
    sw_mode = st.radio("Switch Mode", ["Add", "Edit"], horizontal=True)
    sel_sw = None
    if sw_mode == "Edit" and not sw_data.empty:
        choice = st.selectbox("Select Switch", sw_data['name'])
        sel_sw = sw_data[sw_data['name'] == choice].iloc[0]

    with st.form("sw_form"):
        c1, c2 = st.columns(2)
        name = c1.text_input("Name", value=sel_sw['name'] if sel_sw is not None else "")
        ip = c2.text_input("IP Address", value=sel_sw['ip_address'] if sel_sw is not None else "")
        mac = st.text_input("MAC", value=sel_sw['mac'] if sel_sw is not None else "")
        role = st.selectbox("Role", ["Grandmaster", "Boundary", "Slave"], index=0 if sel_sw is None else ["Grandmaster", "Boundary", "Slave"].index(sel_sw['role']))
        
        if st.form_submit_button("Update Switch"):
            with get_conn() as conn:
                if sw_mode == "Add":
                    conn.execute("INSERT INTO switches (project_id, name, ip_address, mac, role) VALUES (%s,%s,%s,%s,%s)", (p_id, name, ip, mac, role))
                else:
                    conn.execute("UPDATE switches SET name=%s, ip_address=%s, mac=%s, role=%s WHERE id=%s", (name, ip, mac, role, int(sel_sw['id'])))
            st.rerun()

# --- TAB: PORTS (With Port-to-Port Visibility) ---
with tabs[3]:
    with get_conn() as conn:
        sw_list = pd.read_sql(f"SELECT id, name FROM switches WHERE project_id={p_id}", conn)
        sfp_list = pd.read_sql(f"SELECT id, serial FROM sfps WHERE project_id={p_id}", conn)
    
    with st.form("port_entry"):
        col1, col2 = st.columns(2)
        l_sw = col1.selectbox("Local Switch", sw_list['name'])
        l_p = col1.number_input("Local Port #", 1, 18)
        
        r_sw = col2.selectbox("Remote Switch", ["None"] + sw_list['name'].tolist())
        r_p = col2.number_input("Remote Port #", 1, 18)
        
        if st.form_submit_button("Save Link"):
            lid = int(sw_list[sw_list['name'] == l_sw]['id'].values[0])
            rid = int(sw_list[sw_list['name'] == r_sw]['id'].values[0]) if r_sw != "None" else None
            with get_conn() as conn:
                conn.execute("INSERT INTO ports (project_id, switch_id, port_num, connected_to_id, connected_port_num) VALUES (%s,%s,%s,%s,%s)", (p_id, lid, l_p, rid, r_p))
            st.success("Port Link Saved")
