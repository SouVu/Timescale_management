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
            # 1. Projects
            cur.execute("CREATE TABLE IF NOT EXISTS projects (id SERIAL PRIMARY KEY, name TEXT UNIQUE)")
            # 2. Switches
            cur.execute("""CREATE TABLE IF NOT EXISTS switches (
                id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id), 
                name TEXT UNIQUE, role TEXT, ip_address TEXT, mac TEXT)""")
            # 3. SFPs
            cur.execute("""CREATE TABLE IF NOT EXISTS sfps (
                id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id),
                serial TEXT UNIQUE, wavelength TEXT, alpha FLOAT, delta_tx FLOAT, delta_rx FLOAT)""")
            # 4. Ports
            cur.execute("""CREATE TABLE IF NOT EXISTS ports (
                id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id),
                switch_id INTEGER REFERENCES switches(id), port_num INTEGER, sfp_id INTEGER REFERENCES sfps(id),
                connected_to_id INTEGER REFERENCES switches(id), connected_port_num INTEGER,
                port_delta_tx FLOAT DEFAULT 0, port_delta_rx FLOAT DEFAULT 0)""")
            
            # Migration: Ensure columns exist if you updated from an old version
            cur.execute("ALTER TABLE switches ADD COLUMN IF NOT EXISTS mac TEXT")
            cur.execute("ALTER TABLE switches ADD COLUMN IF NOT EXISTS ip_address TEXT")
            cur.execute("ALTER TABLE ports ADD COLUMN IF NOT EXISTS connected_port_num INTEGER")
        conn.commit()

# --- APP SETUP ---
st.set_page_config(layout="wide", page_title="White Rabbit Manager")
init_db()

# --- SIDEBAR: NETWORK SELECTOR ---
st.sidebar.title("📂 Network Selector")
with get_conn() as conn:
    all_projects = pd.read_sql("SELECT * FROM projects", conn)

if all_projects.empty:
    st.sidebar.warning("No networks found.")
    new_p = st.sidebar.text_input("New Network Name")
    if st.sidebar.button("Create Network"):
        with get_conn() as conn: conn.execute("INSERT INTO projects (name) VALUES (%s)", (new_p,))
        st.rerun()
    st.stop()

selected_project = st.sidebar.selectbox("Active Network", all_projects['name'])
p_id = int(all_projects[all_projects['name'] == selected_project]['id'].values[0])

# --- MAIN TABS ---
st.title(f"🐇 {selected_project} Dashboard")
tabs = st.tabs(["🗺️ Map & PDF", "🖥️ Switches", "🔌 SFPs", "⚙️ Connections", "📐 Fiber Calc"])

# --- TAB 1: SWITCHES ---
with tabs[1]:
    st.subheader("Manage Switches")
    with get_conn() as conn: 
        # Load data
        df_sw = pd.read_sql(f"SELECT * FROM switches WHERE project_id={p_id} ORDER BY name", conn)
    
    # 1. Show Data
    st.dataframe(df_sw, use_container_width=True)

    # 2. Add / Edit Form
    with st.form("sw_form"):
        st.write("**Add or Update Switch** (Type existing name to update)")
        c1, c2, c3 = st.columns(3)
        sw_name = c1.text_input("Hostname (e.g. WRS-01)", placeholder="Unique Name")
        sw_ip = c2.text_input("IP Address")
        sw_mac = c3.text_input("MAC Address")
        sw_role = st.selectbox("Role", ["Grandmaster", "Boundary", "Slave"])
        
        if st.form_submit_button("Save Switch"):
            if sw_name:
                with get_conn() as conn:
                    # Upsert Logic: Insert, or Update if name exists
                    conn.execute("""
                        INSERT INTO switches (project_id, name, ip_address, mac, role) 
                        VALUES (%s, %s, %s, %s, %s) 
                        ON CONFLICT (name) DO UPDATE 
                        SET ip_address=EXCLUDED.ip_address, mac=EXCLUDED.mac, role=EXCLUDED.role
                    """, (p_id, sw_name, sw_ip, sw_mac, sw_role))
                st.success(f"Saved {sw_name}")
                st.rerun()
            else:
                st.error("Hostname is required.")

    # 3. Delete Zone
    with st.expander("🗑️ Danger Zone (Delete Switches)"):
        if not df_sw.empty:
            del_sw = st.selectbox("Select Switch to Delete", df_sw['name'])
            if st.button("Permanently Delete Switch"):
                with get_conn() as conn:
                    # Manually delete dependent ports first to avoid SQL errors
                    sw_id_to_del = int(df_sw[df_sw['name'] == del_sw]['id'].values[0])
                    conn.execute("DELETE FROM ports WHERE switch_id = %s OR connected_to_id = %s", (sw_id_to_del, sw_id_to_del))
                    conn.execute("DELETE FROM switches WHERE id = %s", (sw_id_to_del,))
                st.warning(f"Deleted {del_sw}")
                st.rerun()

# --- TAB 2: SFPs ---
with tabs[2]:
    st.subheader("SFP Inventory")
    with get_conn() as conn:
        df_sfp = pd.read_sql(f"SELECT * FROM sfps WHERE project_id={p_id} ORDER BY serial", conn)
    
    st.dataframe(df_sfp, use_container_width=True)

    with st.form("sfp_form"):
        st.write("**Register SFP**")
        ca, cb, cc = st.columns(3)
        s_sn = ca.text_input("Serial Number")
        s_wv = cb.text_input("Wavelength (nm)")
        s_al = cc.number_input("Alpha Parameter", format="%.10f")
        if st.form_submit_button("Save SFP"):
            with get_conn() as conn:
                conn.execute("""
                    INSERT INTO sfps (project_id, serial, wavelength, alpha) 
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (serial) DO UPDATE SET wavelength=EXCLUDED.wavelength, alpha=EXCLUDED.alpha
                """, (p_id, s_sn, s_wv, s_al))
            st.rerun()

    with st.expander("🗑️ Danger Zone (Delete SFPs)"):
        if not df_sfp.empty:
            del_sfp_sn = st.selectbox("Select SFP Serial", df_sfp['serial'])
            if st.button("Delete SFP"):
                with get_conn() as conn:
                    sfp_id_to_del = int(df_sfp[df_sfp['serial'] == del_sfp_sn]['id'].values[0])
                    conn.execute("DELETE FROM ports WHERE sfp_id = %s", (sfp_id_to_del,)) # Clear links first
                    conn.execute("DELETE FROM sfps WHERE id = %s", (sfp_id_to_del,))
                st.warning(f"Deleted SFP {del_sfp_sn}")
                st.rerun()

# --- TAB 3: PORTS ---
with tabs[3]:
    st.subheader("Port Connections")
    with get_conn() as conn:
        # Complex join to make the table readable
        df_p = pd.read_sql(f"""
            SELECT p.id, s1.name as local_switch, p.port_num, s2.name as remote_switch, p.connected_port_num, sfp.serial as sfp
            FROM ports p 
            JOIN switches s1 ON p.switch_id=s1.id 
            LEFT JOIN switches s2 ON p.connected_to_id=s2.id
            LEFT JOIN sfps sfp ON p.sfp_id=sfp.id
            WHERE p.project_id={p_id}
        """, conn)
    
    st.dataframe(df_p, use_container_width=True)

    with st.form("link_form"):
        st.write("**Create Port Link**")
        if not df_sw.empty:
            c1, c2 = st.columns(2)
            l_sw = c1.selectbox("Local Switch", df_sw['name'])
            l_p = c1.number_input("Local Port", 1, 18)
            r_sw = c2.selectbox("Remote Switch", ["None"] + df_sw['name'].tolist())
            r_p = c2.number_input("Remote Port", 1, 18)
            
            if st.form_submit_button("Link Ports"):
                lid = int(df_sw[df_sw['name']==l_sw]['id'].values[0])
                rid = int(df_sw[df_sw['name']==r_sw]['id'].values[0]) if r_sw != "None" else None
                with get_conn() as conn:
                    conn.execute("INSERT INTO ports (project_id, switch_id, port_num, connected_to_id, connected_port_num) VALUES (%s, %s, %s, %s, %s)", (p_id, lid, l_p, rid, r_p))
                st.rerun()
        else:
            st.warning("Add switches first!")

    with st.expander("🗑️ Danger Zone (Remove Links)"):
        if not df_p.empty:
            # Create a label like "SwitchA (P1) -> SwitchB (P2)" for the dropdown
            link_labels = df_p.apply(lambda x: f"ID {x['id']}: {x['local_switch']} P{x['port_num']} -> {x['remote_switch']} P{x['connected_port_num']}", axis=1)
            del_link_label = st.selectbox("Select Link to Remove", link_labels)
            
            if st.button("Remove Connection"):
                # Extract ID from the string "ID 123: ..."
                link_id_to_del = int(del_link_label.split(":")[0].replace("ID ", ""))
                with get_conn() as conn:
                    conn.execute("DELETE FROM ports WHERE id = %s", (link_id_to_del,))
                st.success("Connection removed.")
                st.rerun()

# --- TAB 0: MAP ---
with tabs[0]:
    st.subheader("Network Topology")
    with get_conn() as conn:
        links = pd.read_sql(f"SELECT switch_id, connected_to_id, port_num, connected_port_num FROM ports WHERE project_id={p_id} AND connected_to_id IS NOT NULL", conn)
    
    if not df_sw.empty:
        dot = Digraph(format='pdf')
        dot.attr(rankdir='LR') # Left-to-Right layout
        
        # Draw Nodes
        for _, s in df_sw.iterrows():
            # Label format: Name | Role | IP
            label = f"{s['name']}\n{s['role']}\n{s['ip_address'] or 'No IP'}"
            dot.node(str(s['id']), label, shape='box', style='filled', fillcolor='#e1f5fe' if 'Grandmaster' not in s['role'] else '#c8e6c9')
        
        # Draw Edges
        for _, l in links.iterrows():
            dot.edge(str(l['switch_id']), str(l['connected_to_id']), label=f"P{l['port_num']}:P{l['connected_port_num']}")
        
        st.graphviz_chart(dot)
        
        # PDF Button
        try:
            st.download_button("📥 Download PDF Report", data=dot.pipe(), file_name="topology_report.pdf", mime="application/pdf")
        except Exception as e:
            st.warning("Graphviz binary not found for PDF generation (Map is still visible).")
    else:
        st.info("Add switches in the 'Switches' tab to generate the map.")

# --- TAB 4: CALC ---
with tabs[4]:
    st.subheader("Fiber Delay Calculator")
    d_km = st.number_input("Fiber Length (km)", 0.0, format="%.4f")
    delay = (d_km * 1000 * 1.4682) / 299792458 * 1e9
    st.metric("One-Way Delay", f"{delay:,.2f} ns")
