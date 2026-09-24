import io
import requests
import polyline
import pandas as pd
import geopandas as gpd
import folium
import streamlit as st
from streamlit_folium import st_folium
from shapely.geometry import LineString, box

# =====================================================================
# APP CONFIGURATION & STYLING
# =====================================================================
st.set_page_config(
    page_title="Boulder OSMP Trail Tracker",
    page_icon="🥾",
    layout="wide"
)

st.title("🥾 Boulder OSMP Trail Tracker")
st.markdown("Track your personal trail completion progress against Boulder's official Open Space & Mountain Parks network.")

# --- STRAVA API CREDENTIALS ---
# When deployed, store these in Streamlit Secrets (.streamlit/secrets.toml)
CLIENT_ID = st.secrets.get("STRAVA_CLIENT_ID", "YOUR_STRAVA_CLIENT_ID")
CLIENT_SECRET = st.secrets.get("STRAVA_CLIENT_SECRET", "YOUR_STRAVA_CLIENT_SECRET")
# The redirect URI must match your app URL (e.g., http://localhost:8501 for local testing)
REDIRECT_URI = st.secrets.get("REDIRECT_URI", "http://localhost:8501")

# =====================================================================
# 1. CACHED DATA LOADERS
# =====================================================================
@st.cache_data(ttl=86400) # Cache city trails for 24 hours
def load_boulder_trails():
    url = (
        "https://gis.bouldercolorado.gov/ags_svr2/rest/services/osmp/TrailsNEW/MapServer/4/query"
        "?where=1%3D1&outFields=TrailName,TrailType&returnGeometry=true&f=geojson"
    )
    headers = {"User-Agent": "Mozilla/5.0"}
    res = requests.get(url, headers=headers)
    res.raise_for_status()
    return gpd.read_file(io.BytesIO(res.content))

with st.spinner("Loading Boulder OSMP trail network..."):
    boulder_trails = load_boulder_trails()

# =====================================================================
# 2. STRAVA OAUTH AUTHENTICATION FLOW
# =====================================================================
# Read URL query parameters to check if user redirected back from Strava
query_params = st.query_params
auth_code = query_params.get("code")

if not auth_code:
    # LANDING PAGE: User has not authenticated yet
    st.divider()
    col1, col2 = st.columns([2, 1])
    with col1:
        st.subheader("Connect your Strava Account")
        st.write(
            "Click below to securely authenticate with Strava. This tool reads your public and private "
            "run/hike activities to calculate exact mileage coverage on Boulder OSMP trails."
        )
        strava_auth_url = (
            f"https://www.strava.com/oauth/authorize?client_id={CLIENT_ID}"
            f"&response_type=code&redirect_uri={REDIRECT_URI}"
            f"&approval_prompt=auto&scope=activity:read_all"
        )
        st.markdown(
            f'<a href="{strava_auth_url}">'
            f'<img src="https://raw.githubusercontent.com/strava/api-documentation/master/images/btn_strava_connectwith_orange.png" alt="Connect with Strava" width="193">'
            f'</a>',
            unsafe_allow_html=True
        )
    with col2:
        st.info("🔒 **Privacy First**: Your password is never shared. Access tokens are used only in memory during your session to render your personal map.")

else:
    # =====================================================================
    # 3. FETCH STRAVA ACTIVITIES FOR AUTHENTICATED USER
    # =====================================================================
    @st.cache_data(show_spinner=False)
    def fetch_strava_user_data(code):
        # Exchange authorization code for an access token
        token_res = requests.post(
            "https://www.strava.com/oauth/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
            },
        ).json()
        
        access_token = token_res.get("access_token")
        if not access_token:
            return None, "Failed to retrieve access token from Strava."

        # Fetch activities
        headers = {"Authorization": f"Bearer {access_token}"}
        activities_url = "https://www.strava.com/api/v3/athlete/activities"
        page = 1
        strava_lines = []

        while True:
            res = requests.get(activities_url, headers=headers, params={"page": page, "per_page": 200})
            if res.status_code != 200:
                break
            acts = res.json()
            if not acts:
                break
            for act in acts:
                poly_str = act.get("map", {}).get("summary_polyline")
                if poly_str:
                    coords = polyline.decode(poly_str)
                    if len(coords) >= 2:
                        strava_lines.append(LineString([(pt[1], pt[0]) for pt in coords]))
            page += 1

        if not strava_lines:
            return None, "No activities found in your Strava account."

        return gpd.GeoDataFrame(geometry=strava_lines, crs="EPSG:4326"), None

    with st.spinner("Fetching your Strava activities and analyzing spatial coverage..."):
        strava_gdf, error_msg = fetch_strava_user_data(auth_code)

    if error_msg:
        st.error(error_msg)
        st.stop()

    # =====================================================================
    # 4. FAST SPATIAL ANALYSIS & CLIPPING ENGINE
    # =====================================================================
    minx, miny, maxx, maxy = boulder_trails.total_bounds
    boulder_bbox = box(minx - 0.08, miny - 0.08, maxx + 0.08, maxy + 0.08)
    strava_boulder = strava_gdf[strava_gdf.geometry.intersects(boulder_bbox)].copy()

    if strava_boulder.empty:
        st.warning("No Strava activities found in the Boulder OSMP geographical area.")
        completed_df = gpd.GeoDataFrame(columns=boulder_trails.columns, crs="EPSG:4326")
        remaining_df = boulder_trails.copy()
        completed_mi, total_mi, pct = 0.0, boulder_trails.to_crs(epsg=3857).geometry.length.sum() / 1609.34, 0.0
    else:
        boulder_proj = boulder_trails.to_crs(epsg=3857)
        strava_proj = strava_boulder.to_crs(epsg=3857)

        # Fast 2D polygon buffering and unioning
        buffered_polygons = strava_proj.geometry.simplify(3.0).buffer(15)
        buffered_union = buffered_polygons.union_all()
        strava_buffer = gpd.GeoDataFrame(geometry=[buffered_union], crs="EPSG:3857")

        # Spatial pre-filtering via sjoin
        touched_idx = gpd.sjoin(boulder_proj, strava_buffer, predicate='intersects').index.unique()
        touched_trails = boulder_proj.loc[touched_idx]
        untouched_trails = boulder_proj.drop(touched_idx)

        # Precise line clipping at turnaround points
        completed_touched = gpd.overlay(touched_trails, strava_buffer, how='intersection')
        remaining_touched = gpd.overlay(touched_trails, strava_buffer, how='difference')

        # --- FILTER JUNCTION STUBS (< 25 METERS / ~82 FEET) ---
        MIN_STUB_METERS = 25
        if not completed_touched.empty:
            valid_mask = completed_touched.geometry.length > MIN_STUB_METERS
            completed_proj = completed_touched[valid_mask].copy()
            
            # Re-attach dropped stubs to remaining trails so red lines stay continuous
            stubs = completed_touched[~valid_mask].copy()
            remaining_proj = pd.concat([untouched_trails, remaining_touched, stubs], ignore_index=True)
        else:
            completed_proj = completed_touched
            remaining_proj = pd.concat([untouched_trails, remaining_touched], ignore_index=True)

        completed_df = completed_proj.to_crs(epsg=4326) if not completed_proj.empty else completed_proj
        remaining_df = remaining_proj.to_crs(epsg=4326)

        total_mi = boulder_proj.geometry.length.sum() / 1609.34
        completed_mi = completed_proj.geometry.length.sum() / 1609.34 if not completed_proj.empty else 0.0
        pct = (completed_mi / total_mi) * 100 if total_mi > 0 else 0.0

    # =====================================================================
    # 5. DASHBOARD METRICS & MAP DISPLAY
    # =====================================================================
    col_a, col_b, col_c = st.columns(3)
    col_a.metric("Total OSMP Network", f"{total_mi:.1f} mi")
    col_b.metric("Completed Distance", f"{completed_mi:.1f} mi")
    col_c.metric("OSMP Progress", f"{pct:.1f}%")

    st.divider()

    # Build Folium map with Esri Topo default baseline
    name_col = next((c for c in boulder_trails.columns if c.lower() == 'trailname'), boulder_trails.columns[0])
    m = folium.Map(location=[40.0000, -105.2820], zoom_start=13, tiles=None)

    # Basemaps
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Esri World Topo", name="Esri Topographic", show=True
    ).add_to(m)
    folium.TileLayer(
        tiles="https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        attr="OpenTopoMap", name="OpenTopoMap (Contours)", show=False
    ).add_to(m)

    # Remaining Trails (Red dashed with white outline)
    rem_group = folium.FeatureGroup(name="Remaining OSMP Trails")
    folium.GeoJson(remaining_df, style_function=lambda f: {'color': '#FFFFFF', 'weight': 5, 'opacity': 0.9}).add_to(rem_group)
    folium.GeoJson(
        remaining_df,
        style_function=lambda f: {'color': '#E74C3C', 'weight': 2.5, 'dashArray': '5, 5', 'opacity': 1.0},
        tooltip=folium.GeoJsonTooltip(fields=[name_col], aliases=['Unvisited Trail:'])
    ).add_to(rem_group)
    rem_group.add_to(m)

    # Completed Trails (Emerald Green with dark casing)
    comp_group = folium.FeatureGroup(name="Completed OSMP Trails")
    folium.GeoJson(completed_df, style_function=lambda f: {'color': '#0F172A', 'weight': 6.5, 'opacity': 0.9}).add_to(comp_group)
    folium.GeoJson(
        completed_df,
        style_function=lambda f: {'color': '#2ECC71', 'weight': 4.0, 'opacity': 1.0},
        tooltip=folium.GeoJsonTooltip(fields=[name_col], aliases=['Completed Trail:'])
    ).add_to(comp_group)
    comp_group.add_to(m)

    # Raw Strava Layer (Togglable)
    strava_layer = folium.FeatureGroup(name="All Raw Strava Activities", show=False)
    folium.GeoJson(strava_gdf, style_function=lambda f: {'color': '#FF5722', 'weight': 1.5, 'opacity': 0.4}).add_to(strava_layer)
    strava_layer.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)

    # Render in Streamlit
    st_folium(
        m, 
        use_container_width=True, 
        height=650, 
        returned_objects=[]
    )