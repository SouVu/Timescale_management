import streamlit as st
import pandas as pd
import json
import psycopg
from graphviz import Digraph

# --- DB CONNECTION ---
# This looks for DB_URI in your local secrets.toml OR the Streamlit Cloud Secrets dashboard
def get_conn():
    return psycopg.connect(st.secrets["DB_URI"])

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 1. White Rabbit Switches
            cur.execute('''CREATE TABLE IF NOT EXISTS switches (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE,
                role TEXT,
                mac TEXT,
                clock_source TEXT,
                remarks TEXT
            )''')
            # 2. SFPs Inventory
            cur.execute('''CREATE TABLE IF NOT EXISTS sfps (
                id SERIAL PRIMARY KEY,
                serial TEXT UNIQUE,
                wavelength TEXT,
                alpha FLOAT, 
                delta_tx FLOAT,
                delta_rx FLOAT,
                remarks TEXT
            )''')
            # 3. Ports & Topology (The Linkage)
            cur.execute('''CREATE TABLE IF NOT EXISTS ports (
                id SERIAL PRIMARY KEY,
                switch_id INTEGER REFERENCES switches(id),
                port_num INTEGER,
                sfp_id INTEGER REFERENCES sfps(id),
                connected_to_id INTEGER REFERENCES switches(id),
                port_delta_tx FLOAT DEFAULT 0,
                port_delta_rx FLOAT DEFAULT 0,
                remarks TEXT
            )''')
        conn.commit()

# --- APP SETUP ---
st.set_page_config(layout="wide", page_title="White Rabbit Manager")
try:
    init_db()
except Exception as e:
    st.error(f"Database Connection Failed. Check your secrets! Error: {e}")
    st.stop()

st.title("🐇 White Rabbit Network Manager")
tabs = st.tabs(["🗺️ Topology Map", "🖥️ Switches", "🔌 SFP Inventory", "⚙️ Port Calibration", "📄 .config Gen"])

# --- TAB: NETWORK MAP ---
with tabs[0]:
    st.subheader("Visual Topology")
    with get_conn() as conn:
        df_sw = pd.read_sql("SELECT id, name, role FROM switches", conn)
        df_links = pd.read_sql("SELECT switch_id, connected_to_id, port_num FROM ports WHERE connected_to_id IS NOT NULL", conn)
    
    if df_sw.empty:
        st.info("Add switches to see the map.")
    else:
        dot = Digraph()
        dot.attr(rankdir='TB', bgcolor='#0e1117', fontcolor='white')
        dot.attr('node', shape='record', style='filled', color='#1f77b4', fontcolor='white')

        for _, s in df_sw.iterrows():
            color = "#2ca02c" if "Grandmaster" in s['role'] else "#1f77b4"
            dot.node(str(s['id']), f"{{ {s['name']} | {s['role']} }}", fillcolor=color)

        for _, l in df_links.iterrows():
            dot.edge(str(l['switch_id']), str(l['connected_to_id']), label=f"Port {l['port_num']}")
        
        st.graphviz_chart(dot)

# --- TAB: SWITCHES ---
with tabs[1]:
    with st.form("sw_form"):
        col1, col2 = st.columns(2)
        name = col1.text_input("Switch Name", placeholder="e.g., WRS-SYD-01")
        role = col2.selectbox("Role", ["Grandmaster", "Boundary Clock", "Slave"])
        mac = st.text_input("MAC Address")
        rem = st.text_area("Remarks")
        if st.form_submit_button("Add Switch"):
            with get_conn() as conn:
                conn.execute("INSERT INTO switches (name, role, mac, remarks) VALUES (%s,%s,%s,%s)", (name, role, mac, rem))
            st.success(f"Switch {name} added!")

# --- TAB: SFPs ---
with tabs[2]:
    with st.form("sfp_form"):
        c1, c2, c3 = st.columns(3)
        sn = c1.text_input("SFP Serial Number")
        wv = c2.text_input("Wavelength (nm)")
        al = c3.number_input("Alpha Parameter", format="%.10f")
        dtx = st.number_input("SFP Delta Tx (ps)")
        drx = st.number_input("SFP Delta Rx (ps)")
        rem_sfp = st.text_area("SFP Remarks")
        if st.form_submit_button("Save SFP"):
            with get_conn() as conn:
                conn.execute("INSERT INTO sfps (serial, wavelength, alpha, delta_tx, delta_rx, remarks) VALUES (%s,%s,%s,%s,%s,%s)", 
                             (sn, wv, al, dtx, drx, rem_sfp))
            st.success("SFP saved to inventory.")

# --- TAB: PORT CALIBRATION ---
with tabs[3]:
    with get_conn() as conn:
        sw_list = pd.read_sql("SELECT id, name FROM switches", conn)
        sfp_list = pd.read_sql("SELECT id, serial FROM sfps", conn)

    if not sw_list.empty:
        with st.form("port_form"):
            target_sw = st.selectbox("Switch", sw_list['name'])
            p_idx = st.number_input("Port Number", 1, 18)
            sfp_sn = st.selectbox("Plugged SFP", ["None"] + sfp_list['serial'].tolist())
            link_to = st.selectbox("Connected to Switch", ["None"] + sw_list['name'].tolist())
            p_dtx = st.number_input("Port PCB Delta Tx (ps)")
            p_drx = st.number_input("Port PCB Delta Rx (ps)")
            p_rem = st.text_area("Port Remarks")
            
            if st.form_submit_button("Update Port"):
                sw_id = int(sw_list[sw_list['name'] == target_sw]['id'].values[0])
                sfp_id = int(sfp_list[sfp_list['serial'] == sfp_sn]['id'].values[0]) if sfp_sn != "None" else None
                link_id = int(sw_list[sw_list['name'] == link_to]['id'].values[0]) if link_to != "None" else None
                
                with get_conn() as conn:
                    conn.execute("""INSERT INTO ports (switch_id, port_num, sfp_id, connected_to_id, port_delta_tx, port_delta_rx, remarks) 
                                 VALUES (%s,%s,%s,%s,%s,%s,%s)""", (sw_id, p_idx, sfp_id, link_id, p_dtx, p_drx, p_rem))
                st.success("Port configured.")

# --- TAB: CONFIG GEN ---
with tabs[4]:
    if not sw_list.empty:
        sel_sw = st.selectbox("Generate Config for:", sw_list['name'])
        if st.button("Generate .config"):
            with get_conn() as conn:
                sw_data = pd.read_sql(f"SELECT * FROM switches WHERE name='{sel_sw}'", conn).iloc[0]
                ports = pd.read_sql(f"""
                    SELECT p.*, s.serial, s.alpha, s.delta_tx as s_tx, s.delta_rx as s_rx 
                    FROM ports p LEFT JOIN sfps s ON p.sfp_id = s.id 
                    WHERE p.switch_id = {sw_data['id']}""", conn)
            
            cfg = [f"# White Rabbit Config: {sw_data['name']}", f"CONFIG_WR_NODE_MAC=\"{sw_data['mac']}\""]
            for _, p in ports.iterrows():
                i = p['port_num']
                cfg.append(f"\n# Port {i} [{p['remarks'] or 'No Remarks'}]")
                cfg.append(f"CONFIG_PORT{i}_SFP_ALPHA={p['alpha'] or 0}")
                # Total Delta = SFP Delta + Port PCB Delta
                cfg.append(f"CONFIG_PORT{i}_DELTA_TX={int((p['s_tx'] or 0) + p['port_delta_tx'])}")
                cfg.append(f"CONFIG_PORT{i}_DELTA_RX={int((p['s_rx'] or 0) + p['port_delta_rx'])}")
            
            st.code("\n".join(cfg))
