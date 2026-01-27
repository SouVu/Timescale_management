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
            cur.execute("CREATE TABLE IF NOT EXISTS projects (id SERIAL PRIMARY KEY, name TEXT UNIQUE, remarks TEXT)")
            cur.execute("""CREATE TABLE IF NOT EXISTS switches (
                id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id), 
                name TEXT, role TEXT, ip_address TEXT, mac TEXT, clock_source TEXT, remarks TEXT)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS sfps (
                id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id),
                serial TEXT UNIQUE, wavelength TEXT, alpha FLOAT, delta_tx FLOAT, delta_rx FLOAT)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS ports (
                id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id),
                switch_id INTEGER REFERENCES switches(id), port_num INTEGER, sfp_id INTEGER REFERENCES sfps(id),
                connected_to_id INTEGER REFERENCES switches(id), connected_port_num INTEGER,
                port_delta_tx FLOAT DEFAULT 0, port_delta_rx FLOAT DEFAULT 0, remarks TEXT)""")
            cur.execute("ALTER TABLE switches ADD COLUMN IF NOT EXISTS ip_address TEXT")
            cur.execute("ALTER TABLE ports ADD COLUMN IF NOT EXISTS connected_port_num INTEGER")
        conn.commit()

# --- APP SETUP ---
st.set_page_config(layout="wide", page_title="White Rabbit NMS")
init_db()

# --- SIDEBAR ---
st.sidebar.title("🔐 Access Control")
is_admin = st.sidebar.toggle("Admin Mode")
if is_admin:
    pwd = st.sidebar.text_input("Admin Password", type="password")
    if pwd != "wr_admin": # Change this as needed
        is_admin = False

st.sidebar.divider()
st.sidebar.title("📂 Network Selection")
with get_conn() as conn:
    all_projects = pd.read_sql("SELECT * FROM projects", conn)

if all_projects.empty:
    st.sidebar.warning("Initialize a network first.")
    new_p_name = st.sidebar.text_input("New Network Name")
    if st.sidebar.button("Create"):
        with get_conn() as conn: conn.execute("INSERT INTO projects (name) VALUES (%s)", (new_p_name,))
        st.rerun()
    st.stop()

selected_project_name = st.sidebar.selectbox("Active Network", all_projects['name'])
p_id = int(all_projects[all_projects['name'] == selected_project_name]['id'].values[0])

# --- CLONE LOGIC (ADMIN ONLY) ---
if is_admin:
    st.sidebar.divider()
    clone_name = st.sidebar.text_input("Clone to New Name")
    if st.sidebar.button("🚀 Clone Network"):
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO projects (name) VALUES (%s) RETURNING id", (clone_name,))
                new_p_id = cur.fetchone()[0]
                cur.execute("INSERT INTO sfps (project_id, serial, wavelength, alpha, delta_tx, delta_rx) SELECT %s, serial || '-' || %s, wavelength, alpha, delta_tx, delta_rx FROM sfps WHERE project_id = %s", (new_p_id, clone_name, p_id))
                cur.execute("SELECT id, name, role, ip_address, mac FROM switches WHERE project_id = %s", (p_id,))
                old_sws = cur.fetchall()
                sw_map = {}
                for oid, name, role, ip, mac in old_sws:
                    cur.execute("INSERT INTO switches (project_id, name, role, ip_address, mac) VALUES (%s,%s,%s,%s,%s) RETURNING id", (new_p_id, name, role, ip, mac))
                    sw_map[oid] = cur.fetchone()[0]
                cur.execute("SELECT switch_id, port_num, connected_to_id, connected_port_num FROM ports WHERE project_id = %s", (p_id,))
                for sid, pnum, cid, cpnum in cur.fetchall():
                    new_sid = sw_map.get(sid)
                    new_cid = sw_map.get(cid) if cid else None
                    cur.execute("INSERT INTO ports (project_id, switch_id, port_num, connected_to_id, connected_port_num) VALUES (%s,%s,%s,%s,%s)", (new_p_id, new_sid, pnum, new_cid, cpnum))
            conn.commit()
            st.rerun()

# --- MAIN TABS ---
st.title(f"🐇 {selected_project_name}")
tabs = st.tabs(["🗺️ Map & Export", "🖥️ Switches", "🔌 SFPs", "⚙️ Ports", "📐 Fiber Calc", "📄 Config"])

# --- TAB 0: MAP & PDF EXPORT ---
with tabs[0]:
    with get_conn() as conn:
        df_sw = pd.read_sql(f"SELECT id, name, role, ip_address FROM switches WHERE project_id={p_id}", conn)
        df_links = pd.read_sql(f"SELECT switch_id, connected_to_id, port_num, connected_port_num FROM ports WHERE project_id={p_id} AND connected_to_id IS NOT NULL", conn)
    
    if not df_sw.empty:
        # Create Graph
        dot = Digraph(format='pdf')
        dot.attr(rankdir='LR', bgcolor='#f0f2f6')
        dot.attr('node', shape='record', style='filled', fillcolor='#1f77b4', fontcolor='white', fontname='Arial')

        for _, s in df_sw.iterrows():
            dot.node(str(s['id']), f"{{ {s['name']} | {s['role']} | {s['ip_address']} }}")
        for _, l in df_links.iterrows():
            dot.edge(str(l['switch_id']), str(l['connected_to_id']), label=f"P{l['port_num']} : P{l['connected_port_num']}")
        
        # Display in App
        st.graphviz_chart(dot)

        # PDF Export Button
        st.divider()
        st.subheader("🖨️ Export Report")
        pdf_data = dot.pipe()
        st.download_button(
            label="Download Network Map as PDF",
            data=pdf_data,
            file_name=f"WR_Topology_{selected_project_name}.pdf",
            mime="application/pdf"
        )
    else:
        st.info("Start by adding switches in the next tab.")

# --- TAB 1: SWITCHES ---
with tabs[1]:
    with get_conn() as conn: sw_data = pd.read_sql(f"SELECT * FROM switches WHERE project_id={p_id}", conn)
    if is_admin:
        with st.form("sw_form"):
            c1, c2 = st.columns(2)
            name = c1.text_input("Switch Name")
            ip = c2.text_input("IP Address")
            role = st.selectbox("Role", ["Grandmaster", "Boundary", "Slave"])
            if st.form_submit_button("Add Switch"):
                with get_conn() as conn: conn.execute("INSERT INTO switches (project_id, name, ip_address, role) VALUES (%s,%s,%s,%s)", (p_id, name, ip, role))
                st.rerun()
    st.dataframe(sw_data, use_container_width=True)

# --- TAB 3: PORTS (Simplified for brevity, includes local/remote mapping) ---
with tabs[3]:
    with get_conn() as conn:
        sw_list = pd.read_sql(f"SELECT id, name FROM switches WHERE project_id={p_id}", conn)
    if is_admin and not sw_list.empty:
        with st.form("port_form"):
            c1, c2 = st.columns(2)
            l_sw = c1.selectbox("From Switch", sw_list['name'])
            l_p = c1.number_input("From Port", 1, 18)
            r_sw = c2.selectbox("To Switch", sw_list['name'])
            r_p = c2.number_input("To Port", 1, 18)
            if st.form_submit_button("Create Link"):
                lid = int(sw_list[sw_list['name']==l_sw]['id'].values[0])
                rid = int(sw_list[sw_list['name']==r_sw]['id'].values[0])
                with get_conn() as conn:
                    conn.execute("INSERT INTO ports (project_id, switch_id, port_num, connected_to_id, connected_port_num) VALUES (%s,%s,%s,%s,%s)", (p_id, lid, l_p, rid, r_p))
                st.rerun()

# --- TAB 4: FIBER CALC ---
with tabs[4]:
    st.subheader("📐 Fiber Delay Calculator")
    l_km = st.number_input("Fiber Length (km)", 0.0, format="%.3f")
    n = 1.4682
    delay = (l_km * 1000 * n) / 299792458 * 1e9
    st.latex(r"\text{Delay (ns)} = \frac{L \times 1000 \times n}{c} \times 10^9")
    st.metric("One-Way Propagation Delay", f"{delay:,.2f} ns")
