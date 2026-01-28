import streamlit as st
import pandas as pd
import psycopg
import io
import zipfile
import json
from graphviz import Digraph

# --- 1. ROBUST DATABASE CONNECTION ---
@st.cache_resource
def get_db_connection():
    """Establishes a persistent connection to PostgreSQL."""
    try:
        # Connect to Postgres
        return psycopg.connect(st.secrets["DB_URI"], autocommit=True)
    except Exception as e:
        st.error(f"❌ Critical Database Error: {e}")
        st.stop()

conn = get_db_connection()

# --- 2. HELPERS ---
def run_query(query, params=None, fetch=False):
    """Safe query runner with error reporting."""
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
    """Get Pandas DataFrame safely."""
    try:
        return pd.read_sql(query, conn, params=params)
    except Exception as e:
        st.error(f"Data Load Error: {e}")
        return pd.DataFrame()

def init_db():
    """Initialize DB with JSONB support for dynamic fields."""
    with conn.cursor() as cur:
        # Projects now track custom field keys
        cur.execute("""CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY, 
            name TEXT UNIQUE, 
            custom_schema JSONB DEFAULT '{}'::jsonb
        )""")
        
        # Switches with JSONB metadata
        cur.execute("""CREATE TABLE IF NOT EXISTS switches (
            id SERIAL PRIMARY KEY, 
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE, 
            name TEXT, 
            role TEXT, 
            ip_address TEXT, 
            mac TEXT, 
            clock_source TEXT,
            metadata JSONB DEFAULT '{}'::jsonb,
            UNIQUE(project_id, name)
        )""")
        
        # SFPs
        cur.execute("""CREATE TABLE IF NOT EXISTS sfps (
            id SERIAL PRIMARY KEY, 
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            serial TEXT, 
            wavelength TEXT, 
            channel TEXT, 
            alpha FLOAT DEFAULT 0, 
            delta_tx FLOAT DEFAULT 0, 
            delta_rx FLOAT DEFAULT 0, 
            metadata JSONB DEFAULT '{}'::jsonb,
            UNIQUE(project_id, serial)
        )""")
        
        # Ports
        cur.execute("""CREATE TABLE IF NOT EXISTS ports (
            id SERIAL PRIMARY KEY, 
            project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            switch_id INTEGER REFERENCES switches(id) ON DELETE CASCADE, 
            port_num INTEGER, 
            sfp_id INTEGER REFERENCES sfps(id) ON DELETE SET NULL,
            remote_sfp_id INTEGER REFERENCES sfps(id) ON DELETE SET NULL,
            connected_to_id INTEGER REFERENCES switches(id) ON DELETE SET NULL, 
            connected_port_num INTEGER,
            port_delta_tx FLOAT DEFAULT 0, 
            port_delta_rx FLOAT DEFAULT 0,
            vlan INTEGER,
            metadata JSONB DEFAULT '{}'::jsonb
        )""")

# --- 3. LOGIC: DUPLICATION & DYNAMIC FIELDS ---
def duplicate_network(old_pid, new_name):
    """Deep copy of a network."""
    try:
        with conn.cursor() as cur:
            # 1. Create New Project
            cur.execute("INSERT INTO projects (name, custom_schema) SELECT %s, custom_schema FROM projects WHERE id=%s RETURNING id", (new_name, old_pid))
            new_pid = cur.fetchone()[0]

            # 2. Copy Switches & Map IDs
            cur.execute("SELECT id, name, role, ip_address, mac, clock_source, metadata FROM switches WHERE project_id=%s", (old_pid,))
            switches = cur.fetchall()
            sw_map = {} # old_id -> new_id
            for s in switches:
                cur.execute("""INSERT INTO switches (project_id, name, role, ip_address, mac, clock_source, metadata) 
                               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""", 
                               (new_pid, s[1], s[2], s[3], s[4], s[5], s[6]))
                sw_map[s[0]] = cur.fetchone()[0]

            # 3. Copy SFPs & Map IDs
            cur.execute("SELECT id, serial, wavelength, channel, alpha, delta_tx, delta_rx, metadata FROM sfps WHERE project_id=%s", (old_pid,))
            sfps = cur.fetchall()
            sfp_map = {}
            for s in sfps:
                cur.execute("""INSERT INTO sfps (project_id, serial, wavelength, channel, alpha, delta_tx, delta_rx, metadata) 
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""", 
                               (new_pid, s[1], s[2], s[3], s[4], s[5], s[6], s[7]))
                sfp_map[s[0]] = cur.fetchone()[0]

            # 4. Copy Ports (Translate IDs)
            cur.execute("SELECT switch_id, port_num, sfp_id, remote_sfp_id, connected_to_id, connected_port_num, port_delta_tx, port_delta_rx, vlan, metadata FROM ports WHERE project_id=%s", (old_pid,))
            ports = cur.fetchall()
            for p in ports:
                sid = sw_map.get(p[0])
                sfpid = sfp_map.get(p[2])
                rsfpid = sfp_map.get(p[3])
                cid = sw_map.get(p[4])
                if sid:
                    cur.execute("""INSERT INTO ports (project_id, switch_id, port_num, sfp_id, remote_sfp_id, connected_to_id, connected_port_num, port_delta_tx, port_delta_rx, vlan, metadata)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", 
                                   (new_pid, sid, p[1], sfpid, rsfpid, cid, p[5], p[6], p[7], p[8], p[9]))
        return True
    except Exception as e:
        st.error(f"Duplication Failed: {e}")
        return False

# --- 4. APP SETUP ---
st.set_page_config(layout="wide", page_title="White Rabbit Platinum")
init_db()

# --- SIDEBAR: PROJECT MANAGEMENT ---
st.sidebar.title("🎛️ Manager")

# Project Selection
projects = get_df("SELECT * FROM projects ORDER BY id")
if projects.empty:
    st.warning("No networks found.")
    p_name = st.text_input("Create First Network")
    if st.button("Create"):
        run_query("INSERT INTO projects (name) VALUES (%s)", (p_name,))
        st.rerun()
    st.stop()

sel_p_name = st.sidebar.selectbox("Select Network", projects['name'])
pid = int(projects[projects['name'] == sel_p_name]['id'].values[0])

# --- DYNAMIC FIELD CONFIG ---
# Load schema: {"switch": ["Jitter", "Location"], "sfp": ["Vendor"]}
current_schema = projects[projects['id']==pid]['custom_schema'].values[0]
if not current_schema: current_schema = {}

with st.sidebar.expander("🛠️ Custom Fields"):
    st.write("Add fields to your forms dynamically.")
    new_field_type = st.selectbox("Target", ["Switch", "SFP", "Port"])
    new_field_name = st.text_input("Field Name (e.g. Jitter)")
    if st.button("Add Field"):
        key = new_field_type.lower()
        if key not in current_schema: current_schema[key] = []
        if new_field_name and new_field_name not in current_schema[key]:
            current_schema[key].append(new_field_name)
            run_query("UPDATE projects SET custom_schema = %s WHERE id=%s", (json.dumps(current_schema), pid))
            st.rerun()
    
    # Show active fields
    st.write("Active Fields:", current_schema)

# --- DUPLICATION & DELETE ---
with st.sidebar.expander("⚡ Actions"):
    st.write("**Duplicate Network**")
    dup_name = st.text_input("New Name for Copy")
    if st.button("Duplicate"):
        if duplicate_network(pid, dup_name):
            st.success("Network Duplicated!")
            st.rerun()
            
    st.divider()
    if st.button("DELETE NETWORK", type="primary"):
        run_query("DELETE FROM projects WHERE id=%s", (pid,))
        st.rerun()

# --- TABS ---
st.title(f"🐇 {sel_p_name}")
tabs = st.tabs(["🗺️ Map", "🖥️ Switches", "🔌 SFPs", "⚙️ Connections", "💾 Backup", "📐 Calc"])

# --- TAB 1: SWITCHES ---
with tabs[1]:
    st.subheader("Switches")
    # Fetch Data
    sw_df = get_df("SELECT * FROM switches WHERE project_id=%s ORDER BY name", (pid,))
    
    # Flatten metadata for display
    display_df = sw_df.copy()
    if not display_df.empty:
        meta_df = pd.json_normalize(display_df['metadata'])
        display_df = display_df.drop(columns=['metadata']).join(meta_df)
    
    st.dataframe(display_df, use_container_width=True)

    # Form
    with st.form("sw_form"):
        st.write("### Add / Update Switch")
        c1, c2, c3 = st.columns(3)
        s_name = c1.text_input("Name (Unique)", placeholder="WRS-1")
        s_ip = c2.text_input("IP Address")
        s_mac = c3.text_input("MAC Address")
        
        c4, c5 = st.columns(2)
        s_role = c4.selectbox("Role", ["Grandmaster", "Boundary", "Slave"])
        s_clk = c5.text_input("Clock Source")

        # Dynamic Fields Loop
        custom_vals = {}
        if "switch" in current_schema:
            st.write("#### Custom Fields")
            cols = st.columns(3)
            for i, field in enumerate(current_schema["switch"]):
                # Retrieve existing value if editing? (Simple version: just input)
                custom_vals[field] = cols[i % 3].text_input(field)

        if st.form_submit_button("Save Switch"):
            if s_name:
                # Check if exists to decide Update vs Insert
                exists = not sw_df[sw_df['name'] == s_name].empty
                
                meta_json = json.dumps(custom_vals)
                
                if exists:
                    # Update
                    ok, _ = run_query("""UPDATE switches SET ip_address=%s, mac=%s, role=%s, clock_source=%s, metadata=%s 
                                         WHERE project_id=%s AND name=%s""", 
                                         (s_ip, s_mac, s_role, s_clk, meta_json, pid, s_name))
                    if ok: st.success(f"Updated {s_name}")
                else:
                    # Insert
                    ok, _ = run_query("""INSERT INTO switches (project_id, name, ip_address, mac, role, clock_source, metadata) 
                                         VALUES (%s, %s, %s, %s, %s, %s, %s)""", 
                                         (pid, s_name, s_ip, s_mac, s_role, s_clk, meta_json))
                    if ok: st.success(f"Created {s_name}")
                    else: st.error("Failed to create switch. Name might be duplicate.")
                st.rerun()

    # Delete
    if not sw_df.empty:
        with st.expander("Delete Switch"):
            del_target = st.selectbox("Switch to delete", sw_df['name'])
            if st.button("Confirm Delete"):
                run_query("DELETE FROM switches WHERE project_id=%s AND name=%s", (pid, del_target))
                st.rerun()

# --- TAB 2: SFPs ---
with tabs[2]:
    st.subheader("SFPs")
    sfp_df = get_df("SELECT * FROM sfps WHERE project_id=%s ORDER BY serial", (pid,))
    
    # Flatten metadata
    d_sfp = sfp_df.copy()
    if not d_sfp.empty:
        m_sfp = pd.json_normalize(d_sfp['metadata'])
        d_sfp = d_sfp.drop(columns=['metadata']).join(m_sfp)
    st.dataframe(d_sfp, use_container_width=True)

    with st.form("sfp_form"):
        st.write("### Add / Update SFP")
        c1, c2, c3, c4 = st.columns(4)
        sn = c1.text_input("Serial Number")
        ch = c2.text_input("Channel")
        wv = c3.text_input("Wavelength")
        al = c4.number_input("Alpha", format="%.6f")
        c5, c6 = st.columns(2)
        dtx = c5.number_input("Delta Tx", format="%.4f")
        drx = c6.number_input("Delta Rx", format="%.4f")
        
        # Dynamic Fields
        custom_sfp = {}
        if "sfp" in current_schema:
            st.write("#### Custom Fields")
            cols = st.columns(3)
            for i, field in enumerate(current_schema["sfp"]):
                custom_sfp[field] = cols[i % 3].text_input(field)

        if st.form_submit_button("Save SFP"):
            if sn:
                exists = not sfp_df[sfp_df['serial'] == sn].empty
                meta_json = json.dumps(custom_sfp)
                
                if exists:
                    run_query("""UPDATE sfps SET channel=%s, wavelength=%s, alpha=%s, delta_tx=%s, delta_rx=%s, metadata=%s 
                                 WHERE project_id=%s AND serial=%s""", 
                                 (ch, wv, al, dtx, drx, meta_json, pid, sn))
                    st.success(f"Updated {sn}")
                else:
                    run_query("""INSERT INTO sfps (project_id, serial, channel, wavelength, alpha, delta_tx, delta_rx, metadata) 
                                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""", 
                                 (pid, sn, ch, wv, al, dtx, drx, meta_json))
                    st.success(f"Created {sn}")
                st.rerun()

# --- TAB 3: CONNECTIONS ---
with tabs[3]:
    st.subheader("Connections")
    # Complex join for readable table
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

    # UI Options
    sw_opts = sw_df['name'].tolist() if not sw_df.empty else []
    sfp_opts = ["None"] + sfp_df['serial'].tolist() if not sfp_df.empty else ["None"]
    
    mode = st.radio("Mode", ["New Link", "Edit Link"], horizontal=True)

    if mode == "New Link":
        with st.form("new_link"):
            st.write("#### Create Connection")
            c1, c2, c3 = st.columns(3)
            l_sw = c1.selectbox("Local Switch", sw_opts)
            l_p = c1.number_input("Local Port", 1, 52)
            l_sfp = c1.selectbox("Local SFP", sfp_opts)
            
            c4, c5, c6 = st.columns(3)
            p_dtx = c4.number_input("Port Delta Tx", 0.0)
            p_drx = c5.number_input("Port Delta Rx", 0.0)
            vlan = c6.number_input("VLAN ID", 0, 4096, 0)
            
            st.divider()
            r_sw = st.selectbox("Remote Switch", ["None"] + sw_opts)
            r_p = st.number_input("Remote Port", 1, 52)
            r_sfp = st.selectbox("Remote SFP", sfp_opts)

            # Dynamic Port Fields
            custom_port = {}
            if "port" in current_schema:
                st.write("#### Custom Fields")
                cols = st.columns(3)
                for i, field in enumerate(current_schema["port"]):
                    custom_port[field] = cols[i % 3].text_input(field)

            if st.form_submit_button("Create Link"):
                if not sw_df.empty and l_sw:
                    # Resolve IDs safely
                    lid = int(sw_df[sw_df['name']==l_sw]['id'].values[0])
                    rid = int(sw_df[sw_df['name']==r_sw]['id'].values[0]) if r_sw != "None" else None
                    sid1 = int(sfp_df[sfp_df['serial']==l_sfp]['id'].values[0]) if l_sfp != "None" else None
                    sid2 = int(sfp_df[sfp_df['serial']==r_sfp]['id'].values[0]) if r_sfp != "None" else None
                    
                    ok, _ = run_query("""INSERT INTO ports 
                        (project_id, switch_id, port_num, sfp_id, remote_sfp_id, connected_to_id, connected_port_num, port_delta_tx, port_delta_rx, vlan, metadata) 
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", 
                        (pid, lid, l_p, sid1, sid2, rid, r_p, p_dtx, p_drx, vlan if vlan > 0 else None, json.dumps(custom_port)))
                    if ok: st.success("Link Created")
                    st.rerun()

    elif mode == "Edit Link":
        if not conn_df.empty:
            lbls = conn_df.apply(lambda x: f"ID {x['id']}: {x['local']} P{x['port_num']} -> {x['remote']}", axis=1)
            sel = st.selectbox("Select Link", lbls)
            sel_id = int(sel.split(":")[0].replace("ID ", ""))
            
            with st.form("edit_link"):
                st.write(f"Editing {sel}")
                # Fetch current values would be ideal, but for simplicity we allow overwrite
                n_dtx = st.number_input("New Delta Tx", 0.0)
                n_drx = st.number_input("New Delta Rx", 0.0)
                if st.form_submit_button("Update"):
                    run_query("UPDATE ports SET port_delta_tx=%s, port_delta_rx=%s WHERE id=%s", (n_dtx, n_drx, sel_id))
                    st.success("Updated")
                    st.rerun()
            
            if st.button("Delete Link"):
                run_query("DELETE FROM ports WHERE id=%s", (sel_id,))
                st.rerun()

# --- TAB 0: MAP ---
with tabs[0]:
    if not sw_df.empty:
        dot = Digraph(format='pdf')
        dot.attr(rankdir='LR')
        
        # Nodes
        for _, s in sw_df.iterrows():
            # Parse metadata to show in map if needed?
            # Keeping it simple for now to avoid clutter
            lbl = f"{s['name']}\n{s['role']}\n{s['ip_address']}"
            dot.node(str(s['id']), lbl)
            
        # Edges
        links = get_df("SELECT switch_id, connected_to_id, port_num, connected_port_num, vlan FROM ports WHERE project_id=%s AND connected_to_id IS NOT NULL", (pid,))
        for _, l in links.iterrows():
            vlan_txt = f"\nVLAN: {l['vlan']}" if pd.notna(l['vlan']) else ""
            dot.edge(str(l['switch_id']), str(l['connected_to_id']), label=f"P{l['port_num']}:P{l['connected_port_num']}{vlan_txt}")
            
        st.graphviz_chart(dot)

# --- TAB 4: BACKUP ---
with tabs[4]:
    if st.button("📦 Download Full Backup"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("switches.json", sw_df.to_json(orient='records'))
            zf.writestr("sfps.json", sfp_df.to_json(orient='records'))
            zf.writestr("connections.json", conn_df.to_json(orient='records'))
        st.download_button("Download ZIP", buf.getvalue(), "network_backup.zip", "application/zip")

# --- TAB 5: CALC ---
with tabs[5]:
    st.subheader("Fiber Calculator")
    km = st.number_input("Length (km)", 0.0)
    st.metric("One-Way Delay", f"{(km * 1000 * 1.4682 / 299792458 * 1e9):.2f} ns")
