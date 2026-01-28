import streamlit as st
import pandas as pd
import psycopg
import io
import zipfile
import json
from graphviz import Digraph

# =========================================================
# 1. DATABASE CONNECTION
# =========================================================
@st.cache_resource
def get_db_connection():
    try:
        conn = psycopg.connect(st.secrets["DB_URI"], autocommit=True)
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except Exception as e:
        st.error(f"❌ Database connection failed: {e}")
        st.stop()

conn = get_db_connection()

# =========================================================
# 2. HELPERS
# =========================================================
def run_query(query, params=None, fetch=False):
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            if fetch:
                cols = [d.name for d in cur.description]
                return cur.fetchall(), cols
            return True, None
    except Exception as e:
        st.error(f"SQL Error: {e}")
        return False, None


def get_df(query, params=None):
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            cols = [d.name for d in cur.description]
            return pd.DataFrame(cur.fetchall(), columns=cols)
    except Exception as e:
        st.error(f"Query failed: {e}")
        return pd.DataFrame()

# =========================================================
# 3. DB INIT + MIGRATIONS
# =========================================================
def init_db():
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE,
            custom_schema JSONB DEFAULT '{}'::jsonb
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS switches (
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

        cur.execute("""
        CREATE TABLE IF NOT EXISTS sfps (
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

        cur.execute("""
        CREATE TABLE IF NOT EXISTS ports (
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

# =========================================================
# 4. DUPLICATION
# =========================================================
def duplicate_network(old_pid, new_name):
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (name, custom_schema) "
                "SELECT %s, custom_schema FROM projects WHERE id=%s RETURNING id",
                (new_name, old_pid),
            )
            new_pid = cur.fetchone()[0]

            cur.execute("SELECT * FROM switches WHERE project_id=%s", (old_pid,))
            sw_map = {}
            for r in cur.fetchall():
                cur.execute("""
                    INSERT INTO switches (project_id, name, role, ip_address, mac, clock_source, metadata)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
                """, (new_pid, r[2], r[3], r[4], r[5], r[6], r[7]))
                sw_map[r[0]] = cur.fetchone()[0]

            cur.execute("SELECT * FROM sfps WHERE project_id=%s", (old_pid,))
            sfp_map = {}
            for r in cur.fetchall():
                cur.execute("""
                    INSERT INTO sfps (project_id, serial, wavelength, channel, alpha, delta_tx, delta_rx, metadata)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
                """, (new_pid, r[2], r[3], r[4], r[5], r[6], r[7], r[8]))
                sfp_map[r[0]] = cur.fetchone()[0]

            cur.execute("SELECT * FROM ports WHERE project_id=%s", (old_pid,))
            for r in cur.fetchall():
                cur.execute("""
                    INSERT INTO ports (
                        project_id, switch_id, port_num, sfp_id, remote_sfp_id,
                        connected_to_id, connected_port_num,
                        port_delta_tx, port_delta_rx, vlan, metadata
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    new_pid,
                    sw_map.get(r[2]),
                    r[3],
                    sfp_map.get(r[4]),
                    sfp_map.get(r[5]),
                    sw_map.get(r[6]),
                    r[7],
                    r[8],
                    r[9],
                    r[10],
                    r[11],
                ))
        return True
    except Exception as e:
        st.error(f"Clone failed: {e}")
        return False

# =========================================================
# 5. APP START
# =========================================================
st.set_page_config(layout="wide", page_title="WR Manager V3.1")
init_db()

# =========================================================
# SIDEBAR
# =========================================================
st.sidebar.title("🎛️ Network Manager")

projects = get_df("SELECT * FROM projects ORDER BY id")
if projects.empty:
    st.sidebar.warning("No networks yet")

names = projects["name"].tolist()
selected = st.sidebar.selectbox("Active Network", names) if names else None
if not selected:
    st.stop()

pid = int(projects.loc[projects["name"] == selected, "id"].values[0])

# =========================================================
# MAIN UI
# =========================================================
st.title(f"🐇 {selected}")
tabs = st.tabs(["🗺️ Map", "🖥️ Switches", "🔌 SFPs", "⚙️ Connections", "💾 Backup", "📐 Calc"])

# =========================================================
# MAP
# =========================================================
with tabs[0]:
    sw_df = get_df("SELECT * FROM switches WHERE project_id=%s", (pid,))
    if not sw_df.empty:
        dot = Digraph()
        dot.attr(rankdir="LR")
        for _, s in sw_df.iterrows():
            dot.node(str(s["id"]), f"{s['name']}\n{s['role']}")
        links = get_df(
            "SELECT switch_id, connected_to_id, port_num, connected_port_num "
            "FROM ports WHERE project_id=%s AND connected_to_id IS NOT NULL",
            (pid,),
        )
        for _, l in links.iterrows():
            dot.edge(str(l["switch_id"]), str(l["connected_to_id"]),
                     label=f"P{l['port_num']}:P{l['connected_port_num']}")
        st.graphviz_chart(dot)

# =========================================================
# CALCULATOR
# =========================================================
with tabs[5]:
    km = st.number_input("Length (km)", value=0.0)
    delay = km * 1000 * 1.4682 / 299_792_458 * 1e9
    st.metric("One-way delay", f"{delay:.2f} ns")
