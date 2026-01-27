import streamlit as st
import pandas as pd
import psycopg
import io
import zipfile
from graphviz import Digraph

# --- DB CONNECTION ---
def get_conn():
    return psycopg.connect(st.secrets["DB_URI"])

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Tables
            cur.execute("CREATE TABLE IF NOT EXISTS projects (id SERIAL PRIMARY KEY, name TEXT UNIQUE)")
            cur.execute("""CREATE TABLE IF NOT EXISTS switches (
                id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id), 
                name TEXT UNIQUE, role TEXT, ip_address TEXT, mac TEXT)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS sfps (
                id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id),
                serial TEXT UNIQUE, wavelength TEXT, channel TEXT, alpha FLOAT, delta_tx FLOAT, delta_rx FLOAT)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS ports (
                id SERIAL PRIMARY KEY, project_id INTEGER REFERENCES projects(id),
                switch_id INTEGER REFERENCES switches(id), port_num INTEGER, sfp_id INTEGER REFERENCES sfps(id),
                connected_to_id INTEGER REFERENCES switches(id), connected_port_num INTEGER,
                port_delta_tx FLOAT DEFAULT 0, port_delta_rx FLOAT DEFAULT 0)""")
            
            # Migrations (Auto-Fix Old DBs)
            cur.execute("ALTER TABLE switches ADD COLUMN IF NOT EXISTS mac TEXT")
            cur.execute("ALTER TABLE switches ADD COLUMN IF NOT EXISTS ip_address TEXT")
            cur.execute("ALTER TABLE ports ADD COLUMN IF NOT EXISTS connected_port_num INTEGER")
            cur.execute("ALTER TABLE sfps ADD COLUMN IF NOT EXISTS channel TEXT")
        conn.commit()

# --- APP SETUP ---
st.set_page_config(layout="wide", page_title="White Rabbit Manager")
init_db()

# --- SIDEBAR ---
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
tabs = st.tabs(["🗺️ Map", "🖥️ Switches", "🔌 SFPs", "⚙️ Connections", "💾 Backup", "📐 Calc"])

# --- TAB 1: SWITCHES ---
with tabs[1]:
    st.subheader("Manage Switches")
    with get_conn() as conn: 
        df_sw = pd.read_sql(f"SELECT * FROM switches WHERE project_id={p_id} ORDER BY name", conn)
    st.dataframe(df_sw, use_container_width=True)

    with st.form("sw_form"):
        st.write("**Add / Update Switch**")
        c1, c2, c3 = st.columns(3)
        sw_name = c1.text_input("Hostname", placeholder="e.g. WRS-1")
        sw_ip = c2.text_input("IP Address")
        sw_mac = c3.text_input("MAC Address")
        sw_role = st.selectbox("Role", ["Grandmaster", "Boundary", "Slave"])
        if st.form_submit_button("Save Switch"):
            if sw_name:
                with get_conn() as conn:
                    conn.execute("""INSERT INTO switches (project_id, name, ip_address, mac, role) 
                                 VALUES (%s, %s, %s, %s, %s) 
                                 ON CONFLICT (name) DO UPDATE SET ip_address=EXCLUDED.ip_address, mac=EXCLUDED.mac, role=EXCLUDED.role""", 
                                 (p_id, sw_name, sw_ip, sw_mac, sw_role))
                st.rerun()

    with st.expander("🗑️ Danger Zone"):
        if not df_sw.empty:
            del_sw = st.selectbox("Delete Switch", df_sw['name'])
            if st.button("Confirm Delete Switch"):
                with get_conn() as conn:
                    sid = int(df_sw[df_sw['name']==del_sw]['id'].values[0])
                    conn.execute("DELETE FROM ports WHERE switch_id=%s OR connected_to_id=%s", (sid, sid))
                    conn.execute("DELETE FROM switches WHERE id=%s", (sid,))
                st.rerun()

# --- TAB 2: SFPs ---
with tabs[2]:
    st.subheader("SFP Inventory")
    with get_conn() as conn:
        df_sfp = pd.read_sql(f"SELECT * FROM sfps WHERE project_id={p_id} ORDER BY serial", conn)
    st.dataframe(df_sfp, use_container_width=True)

    with st.form("sfp_form"):
        st.write("**Register SFP**")
        c1, c2, c3, c4 = st.columns(4)
        sn = c1.text_input("Serial Number")
        ch = c2.text_input("Channel")
        wv = c3.text_input("Wavelength")
        al = c4.number_input("Alpha", format="%.10f")
        if st.form_submit_button("Save SFP"):
            if sn:
                with get_conn() as conn:
                    conn.execute("""INSERT INTO sfps (project_id, serial, channel, wavelength, alpha) 
                                 VALUES (%s, %s, %s, %s, %s) ON CONFLICT (serial) DO UPDATE SET channel=EXCLUDED.channel""", (p_id, sn, ch, wv, al))
                st.rerun()

    with st.expander("🗑️ Danger Zone"):
        if not df_sfp.empty:
            del_sfp = st.selectbox("Delete SFP", df_sfp['serial'])
            if st.button("Confirm Delete SFP"):
                with get_conn() as conn:
                    sid = int(df_sfp[df_sfp['serial']==del_sfp]['id'].values[0])
                    conn.execute("DELETE FROM ports WHERE sfp_id=%s", (sid,))
                    conn.execute("DELETE FROM sfps WHERE id=%s", (sid,))
                st.rerun()

# --- TAB 3: CONNECTIONS ---
with tabs[3]:
    st.subheader("Port Links")
    with get_conn() as conn:
        df_p = pd.read_sql(f"""SELECT p.id, s1.name as local, p.port_num, s2.name as remote, p.connected_port_num, sfp.serial, sfp.channel 
                            FROM ports p JOIN switches s1 ON p.switch_id=s1.id LEFT JOIN switches s2 ON p.connected_to_id=s2.id 
                            LEFT JOIN sfps sfp ON p.sfp_id=sfp.id WHERE p.project_id={p_id}""", conn)
    st.dataframe(df_p, use_container_width=True)

    with st.form("link_form"):
        st.write("**New Link**")
        if not df_sw.empty:
            c1, c2 = st.columns(2)
            l_sw = c1.selectbox("Local Switch", df_sw['name'])
            l_p = c1.number_input("Local Port", 1, 18)
            r_sw = c2.selectbox("Remote Switch", ["None"] + df_sw['name'].tolist())
            r_p = c2.number_input("Remote Port", 1, 18)
            sfp_sel = st.selectbox("SFP", ["None"] + df_sfp['serial'].tolist())
            
            if st.form_submit_button("Link"):
                lid = int(df_sw[df_sw['name']==l_sw]['id'].values[0])
                rid = int(df_sw[df_sw['name']==r_sw]['id'].values[0]) if r_sw != "None" else None
                sid = int(df_sfp[df_sfp['serial']==sfp_sel]['id'].values[0]) if sfp_sel != "None" else None
                with get_conn() as conn:
                    conn.execute("INSERT INTO ports (project_id, switch_id, port_num, connected_to_id, connected_port_num, sfp_id) VALUES (%s,%s,%s,%s,%s,%s)", (p_id, lid, l_p, rid, r_p, sid))
                st.rerun()

    with st.expander("🗑️ Danger Zone"):
        if not df_p.empty:
            d_link = st.selectbox("Remove Link ID", df_p['id'])
            if st.button("Delete Link"):
                with get_conn() as conn: conn.execute("DELETE FROM ports WHERE id=%s", (d_link,))
                st.rerun()

# --- TAB 4: BACKUP ---
with tabs[4]:
    st.subheader("💾 Backup Database")
    st.write("Download all your current network data as CSV files for safe keeping.")
    
    if st.button("📦 Generate Backup ZIP"):
        # Create In-Memory ZIP
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            with get_conn() as conn:
                # 1. Switches
                sw_csv = pd.read_sql(f"SELECT * FROM switches WHERE project_id={p_id}", conn).to_csv(index=False)
                zf.writestr("switches.csv", sw_csv)
                # 2. SFPs
                sfp_csv = pd.read_sql(f"SELECT * FROM sfps WHERE project_id={p_id}", conn).to_csv(index=False)
                zf.writestr("sfps.csv", sfp_csv)
                # 3. Ports
                port_csv = pd.read_sql(f"SELECT * FROM ports WHERE project_id={p_id}", conn).to_csv(index=False)
                zf.writestr("ports.csv", port_csv)
        
        st.download_button(
            label="⬇️ Download ZIP Archive",
            data=zip_buffer.getvalue(),
            file_name=f"WR_Backup_{selected_project}.zip",
            mime="application/zip"
        )

# --- TAB 0: MAP ---
with tabs[0]:
    with get_conn() as conn:
        links = pd.read_sql(f"SELECT switch_id, connected_to_id, port_num, connected_port_num FROM ports WHERE project_id={p_id} AND connected_to_id IS NOT NULL", conn)
    if not df_sw.empty:
        dot = Digraph(format='pdf')
        dot.attr(rankdir='LR')
        for _, s in df_sw.iterrows():
            dot.node(str(s['id']), f"{s['name']}\n{s['role']}\n{s['ip_address']}\n{s['mac']}")
        for _, l in links.iterrows():
            dot.edge(str(l['switch_id']), str(l['connected_to_id']), label=f"P{l['port_num']}:P{l['connected_port_num']}")
        st.graphviz_chart(dot)
        try: st.download_button("📥 PDF Map", dot.pipe(), "topology.pdf")
        except: pass

# --- TAB 5: CALC ---
with tabs[5]:
    st.subheader("Fiber Calc")
    d = st.number_input("Km", 0.0)
    st.metric("Delay", f"{(d * 1000 * 1.4682 / 299792458 * 1e9):.2f} ns")
