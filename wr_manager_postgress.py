import streamlit as st
import pandas as pd
import json
import psycopg2
from psycopg2.extras import RealDictCursor

# --- CONFIGURATION ---
# ideally, put this in .streamlit/secrets.toml


def get_connection():
    return psycopg2.connect(st.secrets["postgres"]["url"])

def init_db():
    """Initializes tables in PostgreSQL if they don't exist"""
    conn = get_connection()
    c = conn.cursor()
    
    # 1. SWITCHES (White Rabbits)
    # Note: 'SERIAL' is used for auto-increment in Postgres
    c.execute('''CREATE TABLE IF NOT EXISTS switches (
        id SERIAL PRIMARY KEY,
        name TEXT UNIQUE,
        role TEXT,
        mac TEXT,
        custom_attributes TEXT
    )''')
    
    # 2. SFPS (Inventory)
    c.execute('''CREATE TABLE IF NOT EXISTS sfps (
        id SERIAL PRIMARY KEY,
        serial TEXT UNIQUE,
        wavelength TEXT,
        tx_cal_alpha FLOAT,
        rx_cal_alpha FLOAT,
        custom_attributes TEXT
    )''')
    
    # 3. PORTS
    c.execute('''CREATE TABLE IF NOT EXISTS ports (
        id SERIAL PRIMARY KEY,
        switch_id INTEGER REFERENCES switches(id),
        port_number INTEGER,
        sfp_id INTEGER REFERENCES sfps(id),
        connected_to_switch_id INTEGER REFERENCES switches(id),
        port_cal_tx FLOAT DEFAULT 0.0,
        port_cal_rx FLOAT DEFAULT 0.0
    )''')
    
    conn.commit()
    conn.close()

# --- HELPER: Handle Dynamic JSON Fields ---
def unpack_attributes(json_str):
    try:
        return json.loads(json_str) if json_str else {}
    except:
        return {}

def render_dynamic_fields_input(current_data=None):
    st.markdown("#### 🔧 Custom Attributes")
    current_dict = unpack_attributes(current_data)
    new_keys = st.text_area("Add keys (one per line)", value="\n".join(current_dict.keys()))
    
    updated_dict = {}
    if new_keys:
        for key in new_keys.split("\n"):
            key = key.strip()
            if not key: continue
            val = st.text_input(f"Value for '{key}'", value=current_dict.get(key, ""))
            updated_dict[key] = val
            
    return json.dumps(updated_dict)

# --- MAIN APP ---
st.set_page_config(layout="wide", page_title="White Rabbit Networker (Cloud)")

# Initialize DB on first load
try:
    init_db()
except Exception as e:
    st.error(f"Database Connection Failed: {e}")
    st.stop()

st.title("🐇 White Rabbit Manager (Neon DB)")
tabs = st.tabs(["Network Map", "Add Switch", "SFP Inventory", "Port Manager", "Generate Config"])

# --- TAB 1: NETWORK MAP ---
with tabs[0]:
    st.header("Network Topology")
    conn = get_connection()
    query = """
    SELECT 
        s1.name as "Switch", 
        p.port_number as "Port",
        sfp.serial as "SFP Serial",
        s2.name as "Connected To"
    FROM ports p
    JOIN switches s1 ON p.switch_id = s1.id
    LEFT JOIN switches s2 ON p.connected_to_switch_id = s2.id
    LEFT JOIN sfps sfp ON p.sfp_id = sfp.id
    """
    df = pd.read_sql(query, conn)
    st.dataframe(df, use_container_width=True)
    conn.close()

# --- TAB 2: ADD SWITCH ---
with tabs[1]:
    with st.form("add_switch"):
        st.subheader("Register White Rabbit Device")
        col1, col2 = st.columns(2)
        name = col1.text_input("Hostname", "WR-SW-01")
        role = col2.selectbox("Clock Role", ["Grandmaster", "Boundary Clock", "Slave"])
        mac = st.text_input("Base MAC Address")
        custom_json = render_dynamic_fields_input()
        
        if st.form_submit_button("Save Device"):
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute("INSERT INTO switches (name, role, mac, custom_attributes) VALUES (%s,%s,%s,%s)", 
                             (name, role, mac, custom_json))
                conn.commit()
                st.success(f"Deployed {name}")
            except Exception as e:
                st.error(f"Error: {e}")
            finally:
                conn.close()

# --- TAB 3: SFP INVENTORY ---
with tabs[2]:
    st.info("Manage SFP Inventory")
    with st.form("add_sfp"):
        c1, c2, c3 = st.columns(3)
        sn = c1.text_input("Serial Number")
        wv = c2.text_input("Wavelength (nm)", "1310/1550")
        
        c4, c5 = st.columns(2)
        tx_a = c4.number_input("Tx Alpha", format="%.5f")
        rx_a = c5.number_input("Rx Alpha", format="%.5f")
        sfp_json = render_dynamic_fields_input()
        
        if st.form_submit_button("Add SFP"):
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("INSERT INTO sfps (serial, wavelength, tx_cal_alpha, rx_cal_alpha, custom_attributes) VALUES (%s,%s,%s,%s,%s)",
                         (sn, wv, tx_a, rx_a, sfp_json))
            conn.commit()
            conn.close()
            st.success("SFP Added")

# --- TAB 4: PORT MANAGER ---
with tabs[3]:
    st.subheader("Port Configuration")
    conn = get_connection()
    switches = pd.read_sql("SELECT id, name FROM switches", conn)
    
    if not switches.empty:
        sw_name = st.selectbox("Select Switch", switches['name'])
        sw_id = int(switches[switches['name'] == sw_name]['id'].values[0])
        
        # Get Available SFPs
        avail_sfps = pd.read_sql("SELECT id, serial FROM sfps", conn)
        
        with st.form("port_config"):
            c1, c2 = st.columns(2)
            p_num = c1.number_input("Port Number", min_value=1, max_value=18, step=1)
            
            c3, c4 = st.columns(2)
            p_tx = c3.number_input("Port PCB Tx Delay (ps)", value=0.0)
            p_rx = c4.number_input("Port PCB Rx Delay (ps)", value=0.0)
            
            st.divider()
            sfp_choice = st.selectbox("Plug in SFP", ["None"] + avail_sfps['serial'].tolist())
            
            others = switches[switches['id'] != sw_id]
            link_choice = st.selectbox("Link to Switch", ["Disconnected"] + others['name'].tolist())
            
            if st.form_submit_button("Configure Port"):
                sfp_db_id = int(avail_sfps[avail_sfps['serial'] == sfp_choice]['id'].values[0]) if sfp_choice != "None" else None
                link_db_id = int(others[others['name'] == link_choice]['id'].values[0]) if link_choice != "Disconnected" else None

                cur = conn.cursor()
                # Simple Insert (For production, use upsert/update logic)
                cur.execute("""INSERT INTO ports 
                             (switch_id, port_number, sfp_id, connected_to_switch_id, port_cal_tx, port_cal_rx) 
                             VALUES (%s,%s,%s,%s,%s,%s)""",
                             (sw_id, p_num, sfp_db_id, link_db_id, p_tx, p_rx))
                conn.commit()
                st.success("Port Configured")
    conn.close()

# --- TAB 5: GENERATE CONFIG ---
with tabs[4]:
    st.header("White Rabbit `dot-config` Generator")
    conn = get_connection()
    switches = pd.read_sql("SELECT id, name FROM switches", conn)
    
    target_sw = st.selectbox("Select Target Switch", switches['name']) if not switches.empty else None
    
    if target_sw and st.button("Generate Config"):
        s_data = pd.read_sql(f"SELECT * FROM switches WHERE name='{target_sw}'", conn).iloc[0]
        sw_id = s_data['id']
        
        # Advanced query to join SFP data
        p_data = pd.read_sql(f"""
            SELECT p.*, s.serial as sfp_sn, s.tx_cal_alpha, s.rx_cal_alpha 
            FROM ports p 
            LEFT JOIN sfps s ON p.sfp_id = s.id 
            WHERE p.switch_id={sw_id}
        """, conn)
        
        config_lines = [
            f"# WR CONFIG: {s_data['name']}",
            f"CONFIG_WR_NODE_MAC=\"{s_data['mac']}\""
        ]
        
        for _, port in p_data.iterrows():
            idx = port['port_number']
            config_lines.append(f"\n# PORT {idx}")
            if port['sfp_sn']:
                config_lines.append(f"CONFIG_PORT{idx}_SFP_SERIAL=\"{port['sfp_sn']}\"")
            config_lines.append(f"CONFIG_PORT{idx}_PCB_TX_DELAY={port['port_cal_tx']}")
            config_lines.append(f"CONFIG_PORT{idx}_PCB_RX_DELAY={port['port_cal_rx']}")
            
        st.code("\n".join(config_lines))
    conn.close()
