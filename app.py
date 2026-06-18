import streamlit as st
import folium
from streamlit_folium import st_folium
from shapely.geometry import Polygon, shape, mapping
from shapely.ops import transform, unary_union
import pyproj
import math
import json
import io
import csv
from datetime import datetime
import requests

# Опциональные библиотеки с защитой от ошибок установки
try:
    import ezdxf
    EZDXF_AVAILABLE = True
except ImportError:
    EZDXF_AVAILABLE = False

try:
    import overpy
    OVERPY_AVAILABLE = True
except ImportError:
    OVERPY_AVAILABLE = False

# ============================================================================
# 1. КОНВЕРТАЦИЯ КООРДИНАТ И РАСЧЁТ ПЛОЩАДИ
# ============================================================================

def detect_coordinate_system(geom):
    if geom is None or geom.is_empty:
        return None
    try:
        if geom.geom_type == 'Polygon':
            coords = list(geom.exterior.coords)
        elif geom.geom_type == 'MultiPolygon':
            coords = list(geom.geoms[0].exterior.coords)
        else:
            return None
        if len(coords) < 3:
            return None
        x_coords = [c[0] for c in coords]
        y_coords = [c[1] for c in coords]
        x_min, x_max = min(x_coords), max(x_coords)
        y_min, y_max = min(y_coords), max(y_coords)
        if -180 <= x_min and x_max <= 180 and -90 <= y_min and y_max <= 90:
            return 'geographic'
        else:
            return 'projected'
    except Exception:
        return None

def calculate_geodesic_area(geom_wgs84):
    if geom_wgs84 is None or geom_wgs84.is_empty:
        return 0
    try:
        geod = pyproj.Geod(ellps="WGS84")
        if geom_wgs84.geom_type == 'Polygon':
            coords = list(geom_wgs84.exterior.coords)
            lons = [c[0] for c in coords]
            lats = [c[1] for c in coords]
            _, area = geod.polygon_area_perimeter(lons, lats)
            return abs(area)
        elif geom_wgs84.geom_type == 'MultiPolygon':
            total = 0
            for polygon in geom_wgs84.geoms:
                coords = list(polygon.exterior.coords)
                lons = [c[0] for c in coords]
                lats = [c[1] for c in coords]
                _, area = geod.polygon_area_perimeter(lons, lats)
                total += abs(area)
            return total
    except Exception:
        return geom_wgs84.area
    return 0

def calculate_area_universal(geom):
    if geom is None or geom.is_empty:
        return 0, 'unknown'
    coord_system = detect_coordinate_system(geom)
    if coord_system == 'geographic':
        area = calculate_geodesic_area(geom)
        return area, 'WGS84 (градусы)'
    elif coord_system == 'projected':
        return geom.area, 'Проекция (метры)'
    return 0, 'unknown'

def wgs84_to_meters(geom):
    if geom is None or geom.is_empty:
        return geom
    try:
        project = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
        return transform(project.transform, geom)
    except Exception:
        return geom

def meters_to_wgs84(geom):
    if geom is None or geom.is_empty:
        return geom    try:
        project = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
        return transform(project.transform, geom)
    except Exception:
        return geom

def safe_percentage(part, total):
    if total == 0 or total is None:
        return 0.0
    return (part / total) * 100

# ============================================================================
# 2. ШАБЛОНЫ ПЗЗ И АВТО-ОПРЕДЕЛЕНИЕ ЗОНЫ
# ============================================================================

PZZ_TEMPLATES = {
    "residential_high": {
        "name": "Ж-3 (Многоэтажная жилая)",
        "max_floors": 17, "max_density": 0.45, "min_offset": 8,
        "living_ratio": 0.85, "description": "Многоэтажные жилые дома 9-17 этажей"
    },
    "residential_mid": {
        "name": "Ж-2 (Среднеэтажная жилая)",
        "max_floors": 9, "max_density": 0.40, "min_offset": 6,
        "living_ratio": 0.80, "description": "Среднеэтажные жилые дома 5-9 этажей"
    },
    "residential_low": {
        "name": "Ж-1 (Малоэтажная жилая)",
        "max_floors": 4, "max_density": 0.35, "min_offset": 3,
        "living_ratio": 0.90, "description": "Малоэтажная застройка до 4 этажей"
    },
    "commercial": {
        "name": "ОД-1 (Общественно-деловая)",
        "max_floors": 25, "max_density": 0.60, "min_offset": 5,
        "living_ratio": 0.10, "description": "Коммерческая и офисная застройка"
    },
    "mixed": {
        "name": "ОД-2 (Смешанная)",
        "max_floors": 15, "max_density": 0.50, "min_offset": 6,
        "living_ratio": 0.50, "description": "Жило-коммерческая застройка"
    },
    "industrial": {
        "name": "П-1 (Производственная)",
        "max_floors": 6, "max_density": 0.55, "min_offset": 10,
        "living_ratio": 0.0, "description": "Промышленная зона"
    }
}

def detect_pzz_zone(lon, lat):
    if not OVERPY_AVAILABLE:        return "residential_mid", [], [], 0
    try:
        api = overpy.Overpass()
        query = f"""
        [out:json][timeout:10];
        (
          node(around:200,{lat},{lon})["landuse"];
          way(around:200,{lat},{lon})["landuse"];
          node(around:200,{lat},{lon})["building"];
          way(around:200,{lat},{lon})["building"];
        );
        out body;
        """
        result = api.query(query)
        landuse_tags = []
        building_tags = []
        building_levels = []
        for way in result.ways:
            if 'landuse' in way.tags:
                landuse_tags.append(way.tags['landuse'])
            if 'building' in way.tags:
                building_tags.append(way.tags['building'])
            if 'building:levels' in way.tags:
                try:
                    building_levels.append(int(way.tags['building:levels']))
                except Exception:
                    pass
        for node in result.nodes:
            if 'landuse' in node.tags:
                landuse_tags.append(node.tags['landuse'])
            if 'building' in node.tags:
                building_tags.append(node.tags['building'])
        avg_levels = sum(building_levels) / len(building_levels) if building_levels else 0
        if 'commercial' in landuse_tags or 'retail' in landuse_tags:
            return "commercial", landuse_tags, building_tags, avg_levels
        elif 'industrial' in landuse_tags:
            return "industrial", landuse_tags, building_tags, avg_levels
        elif 'residential' in landuse_tags or len(building_tags) > 0:
            if avg_levels > 12:
                return "residential_high", landuse_tags, building_tags, avg_levels
            elif avg_levels > 5:
                return "residential_mid", landuse_tags, building_tags, avg_levels
            else:
                return "residential_low", landuse_tags, building_tags, avg_levels
        return "mixed", landuse_tags, building_tags, avg_levels
    except Exception:
        return "residential_mid", [], [], 0

def reverse_geocode(lon, lat):
    try:        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=10"
        headers = {"User-Agent": "UrbanPotentialAnalyzer/2.0"}
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            return data.get('display_name', 'Адрес не определён'), data.get('address', {})
        return 'Адрес не определён', {}
    except Exception:
        return 'Ошибка геокодирования', {}

# ============================================================================
# 3. ФУНКЦИИ ЭКСПОРТА
# ============================================================================

def calculate_shadow_length(building_height, latitude=55.75):
    solar_angle = 90 - abs(latitude + 23.45)
    solar_angle_rad = math.radians(max(solar_angle, 5))
    return building_height / math.tan(solar_angle_rad)

def create_obj_file(geometry, height_meters):
    if geometry is None or geometry.is_empty:
        return ""
    try:
        coords = []
        if geometry.geom_type == 'Polygon':
            coords = list(geometry.exterior.coords)
        elif geometry.geom_type == 'MultiPolygon':
            coords = list(geometry.geoms[0].exterior.coords)
        if len(coords) < 3:
            return ""
        min_x = min(c[0] for c in coords)
        min_y = min(c[1] for c in coords)
        lines = []
        lines.append("# Urban Potential Analyzer - 3D Building")
        lines.append(f"# Height: {height_meters:.1f}m")
        lines.append("")
        for x, y in coords[:-1]:
            lines.append(f"v {x - min_x:.2f} {y - min_y:.2f} 0.0")
        for x, y in coords[:-1]:
            lines.append(f"v {x - min_x:.2f} {y - min_y:.2f} {height_meters:.2f}")
        lines.append("")
        n = len(coords) - 1
        if n < 3:
            return ""
        base_face = "f " + " ".join(str(i + 1) for i in range(n))
        lines.append(base_face)
        roof_face = "f " + " ".join(str(i + 1 + n) for i in range(n))
        lines.append(roof_face)
        for i in range(n):
            next_i = (i + 1) % n            side_face = f"f {i + 1} {next_i + 1} {next_i + 1 + n} {i + 1 + n}"
            lines.append(side_face)
        return "\n".join(lines)
    except Exception:
        return ""

def create_dxf_file(parcel_geom, buildable_geom, tep, financials, building_height, coord_system='projected'):
    if not EZDXF_AVAILABLE:
        return None
    try:
        doc = ezdxf.new('R2010')
        doc.header['$INSUNITS'] = 6
        doc.layers.add('PARCEL', color=1)
        doc.layers.add('BUILDABLE', color=5)
        doc.layers.add('BUILDING_3D', color=3)
        doc.layers.add('TEXT', color=7)
        msp = doc.modelspace()

        def get_plot_coords(geom):
            if geom.geom_type == 'Polygon':
                coords = list(geom.exterior.coords)
            else:
                coords = list(geom.geoms[0].exterior.coords)
            if coord_system == 'geographic':
                cx = sum(c[0] for c in coords) / len(coords)
                cy = sum(c[1] for c in coords) / len(coords)
                return [
                    ((c[0] - cx) * 111000 * math.cos(math.radians(cy)),
                     (c[1] - cy) * 111000)
                    for c in coords
                ]
            else:
                min_x = min(c[0] for c in coords)
                min_y = min(c[1] for c in coords)
                return [(c[0] - min_x, c[1] - min_y) for c in coords]

        if parcel_geom and not parcel_geom.is_empty:
            coords = get_plot_coords(parcel_geom)
            msp.add_lwpolyline(coords, dxfattribs={'layer': 'PARCEL', 'const_width': 2})

        if buildable_geom and not buildable_geom.is_empty:
            coords = get_plot_coords(buildable_geom)
            msp.add_lwpolyline(coords, dxfattribs={'layer': 'BUILDABLE', 'const_width': 1})
            for i in range(len(coords) - 1):
                p1 = (coords[i][0], coords[i][1], 0)
                p2 = (coords[i+1][0], coords[i+1][1], 0)
                p3 = (coords[i+1][0], coords[i+1][1], building_height)
                p4 = (coords[i][0], coords[i][1], building_height)
                msp.add_3dface([p1, p2, p3, p4], dxfattribs={'layer': 'BUILDING_3D'})
        text_x, text_y = 20, -20
        texts = [
            "=== URBAN ANALYSIS ===",
            f"Area: {tep['s_uch']:,.0f} m2",
            f"Buildable: {tep['s_buildable']:,.0f} m2",
            f"GBA: {tep['s_total']:,.0f} m2",
            f"Floors: {tep['floors']} ({building_height}m)",
            f"Population: {tep['population']}",
            f"ROI: {financials['roi_percent']}%"
        ]
        for i, t in enumerate(texts):
            msp.add_text(t, height=3, dxfattribs={'layer': 'TEXT', 'insert': (text_x, text_y - i*8)})

        buffer = io.BytesIO()
        doc.write(buffer)
        buffer.seek(0)
        return buffer.getvalue()
    except Exception:
        return None

# ============================================================================
# 4. КЛАСС АНАЛИЗАТОРА
# ============================================================================

class UrbanPotentialAnalyzer:
    def __init__(self, parcel_geom, pzz_config, coord_system='auto', restrictions_geom=None):
        self.parcel_geom = parcel_geom
        self.pzz = pzz_config
        self.restrictions = restrictions_geom
        self.coord_system = coord_system if coord_system != 'auto' else detect_coordinate_system(parcel_geom)
        self.buildable_geom = None
        self.buildable_area = 0
        self.tep = {}
        self.financials = {}
        self.real_area, _ = calculate_area_universal(parcel_geom)

    def calculate_buildable_area(self):
        min_offset = self.pzz.get("min_offset_from_border", 0)
        if self.coord_system == 'geographic':
            work_geom = wgs84_to_meters(self.parcel_geom)
        else:
            work_geom = self.parcel_geom
        buildable = work_geom.buffer(-min_offset)
        if self.restrictions is not None and not self.restrictions.is_empty:
            if self.coord_system == 'geographic':
                restrictions_work = wgs84_to_meters(self.restrictions)
            else:
                restrictions_work = self.restrictions
            buildable = buildable.difference(restrictions_work)
        self.buildable_geom = buildable        self.buildable_area, _ = calculate_area_universal(buildable)
        return buildable

    def calculate_tep(self):
        if self.buildable_geom is None:
            self.calculate_buildable_area()
        s_uch = self.real_area
        s_buildable = self.buildable_area
        max_density = self.pzz.get("max_building_density", 1.0)
        max_floors = self.pzz.get("max_floors", 1)
        floor_height = self.pzz.get("floor_height", 3.2)
        living_ratio = self.pzz.get("living_area_ratio", 0.7)
        norm_housing = self.pzz.get("norm_housing_per_person", 28.0)
        s_zas_max = s_uch * max_density
        s_zas = min(s_buildable, s_zas_max)
        s_total = s_zas * max_floors
        s_living = s_total * living_ratio
        s_commercial = s_total - s_living
        population = math.floor(s_living / norm_housing) if norm_housing > 0 else 0
        self.tep = {
            "s_uch": round(s_uch, 1),
            "s_uch_ha": round(s_uch / 10000, 2) if s_uch > 0 else 0,
            "s_buildable": round(s_buildable, 1),
            "s_zas": round(s_zas, 1),
            "s_total": round(s_total, 1),
            "s_living": round(s_living, 1),
            "s_commercial": round(s_commercial, 1),
            "floors": max_floors,
            "building_height_m": round(max_floors * floor_height, 1),
            "population": population,
            "schools": math.ceil((population / 1000) * 120),
            "kindergartens": math.ceil((population / 1000) * 60),
            "parking": math.ceil((s_total / 100) * 1.2) if s_total > 0 else 0,
            "green_space": math.ceil(population * 6),
            "coord_system": self.coord_system
        }
        return self.tep

    def calculate_financials(self):
        if not self.tep:
            self.calculate_tep()
        cost_per_sqm = self.pzz.get("construction_cost_per_sqm", 90000)
        sale_price_per_sqm = self.pzz.get("sale_price_per_sqm", 250000)
        commercial_price_ratio = self.pzz.get("commercial_price_ratio", 0.85)
        land_cost = self.pzz.get("land_cost", 50000000)
        s_living = self.tep["s_living"]
        s_commercial = self.tep["s_commercial"]
        revenue_living = s_living * sale_price_per_sqm
        revenue_commercial = s_commercial * sale_price_per_sqm * commercial_price_ratio
        total_revenue = revenue_living + revenue_commercial        construction_cost = self.tep["s_total"] * cost_per_sqm
        infra_cost = (
            self.tep["schools"] * 1500000 +
            self.tep["kindergartens"] * 1200000 +
            self.tep["parking"] * 800000
        )
        total_cost = construction_cost + land_cost + infra_cost
        gross_profit = total_revenue - total_cost
        roi = (gross_profit / total_cost * 100) if total_cost > 0 else 0
        profit_per_sqm = (gross_profit / self.tep["s_total"]) if self.tep["s_total"] > 0 else 0
        self.financials = {
            "revenue_living": round(revenue_living, 0),
            "revenue_commercial": round(revenue_commercial, 0),
            "total_revenue": round(total_revenue, 0),
            "construction_cost": round(construction_cost, 0),
            "infra_cost": round(infra_cost, 0),
            "land_cost": round(land_cost, 0),
            "total_cost": round(total_cost, 0),
            "gross_profit": round(gross_profit, 0),
            "roi_percent": round(roi, 1),
            "profit_per_sqm": round(profit_per_sqm, 0)
        }
        return self.financials

# ============================================================================
# 5. STREAMLIT UI
# ============================================================================

st.set_page_config(page_title="Urban Potential Analyzer PRO v2.1", layout="wide")
st.title("🏙️ Urban Potential Analyzer PRO v2.1")
st.markdown("**Enterprise Edition:** Авто-ПЗЗ + DXF + точный расчёт площадей")

st.header("📂 Загрузка территории")
data_source = st.radio(
    "Источник данных:",
    ["Тестовый участок", "Загрузить GeoJSON"],
    horizontal=True
)

parcel_geom = None
restrictions_geom = None
coord_system_detected = None

if data_source == "Загрузить GeoJSON":
    st.info("💡 Поддерживаются файлы в WGS84 (градусы) и в проекциях (метры — МСК, UTM, ПУЛКОВО)")
    uploaded_file = st.file_uploader(
        "Загрузите GeoJSON с границами участка",
        type=['geojson', 'json'],
        key="main_parcel"
    )    if uploaded_file is not None:
        try:
            geojson_data = json.loads(uploaded_file.getvalue().decode("utf-8"))
            if geojson_data['type'] == 'FeatureCollection':
                geom_dict = geojson_data['features'][0]['geometry']
            elif geojson_data['type'] == 'Feature':
                geom_dict = geojson_data['geometry']
            else:
                geom_dict = geojson_data
            parcel_geom = shape(geom_dict)
            real_area, coord_sys = calculate_area_universal(parcel_geom)
            coord_system_detected = coord_sys
            st.success(f"✅ Участок загружен!")
            st.info(f"📐 **Площадь:** `{real_area:,.0f} м²` (`{real_area/10000:.2f} га`)  \n🗺️ **Система координат:** `{coord_sys}`")
        except Exception as e:
            st.error(f"❌ Ошибка: {str(e)}")
else:
    st.info("Используется демонстрационный участок (Москва)")
    coords_wgs84 = [
        (37.61500, 55.75200),
        (37.61750, 55.75200),
        (37.61750, 55.75350),
        (37.61500, 55.75350)
    ]
    parcel_geom = Polygon(coords_wgs84)
    real_area, coord_sys = calculate_area_universal(parcel_geom)
    coord_system_detected = coord_sys

# ============================================================================
# АНАЛИЗ И РАСЧЁТЫ
# ============================================================================

if parcel_geom is not None:
    st.header("🏛️ Авто-определение зоны ПЗЗ")
    centroid = parcel_geom.centroid
    if coord_system_detected == 'geographic':
        center_lon, center_lat = centroid.x, centroid.y
    else:
        center_lon, center_lat = 37.6, 55.75
    with st.spinner("Анализирую окружение через OpenStreetMap..."):
        zone_key, landuses, buildings, avg_levels = detect_pzz_zone(center_lon, center_lat)
        suggested_pzz = PZZ_TEMPLATES[zone_key]
        address, _ = reverse_geocode(center_lon, center_lat)
    col1, col2 = st.columns([2, 1])
    with col1:
        st.success(f"🎯 **Предложенная зона:** `{suggested_pzz['name']}`")
        st.write(f"**Описание:** {suggested_pzz['description']}")
        st.caption(f"📍 Адрес: {address[:80]}...")
    with col2:
        st.metric("Средняя этажность рядом", f"{avg_levels:.1f} эт" if avg_levels > 0 else "N/A")        st.caption(f"Найдено объектов: {len(buildings)}")
    with st.expander("🔍 Детали анализа окружения"):
        st.write(f"**Landuse теги:** {', '.join(set(landuses)) if landuses else 'не найдено'}")
        st.write(f"**Building теги:** {', '.join(set(buildings)) if buildings else 'не найдено'}")
    use_suggested = st.checkbox("Использовать предложенные параметры ПЗЗ", value=True)

    st.sidebar.header("⚙️ Градостроительные параметры")
    if use_suggested:
        d_floors = suggested_pzz['max_floors']
        d_density = suggested_pzz['max_density']
        d_offset = suggested_pzz['min_offset']
        d_living = suggested_pzz['living_ratio']
    else:
        d_floors, d_density, d_offset, d_living = 9, 0.4, 5, 0.75
    offset = st.sidebar.slider("Мин. отступ от границ (м)", 0, 30, d_offset)
    density = st.sidebar.slider("Макс. плотность застройки", 0.1, 1.0, d_density, 0.05)
    floors = st.sidebar.slider("Предельная этажность", 1, 50, d_floors)
    floor_height = st.sidebar.slider("Высота этажа (м)", 2.8, 4.5, 3.2, 0.1)
    living_ratio = st.sidebar.slider("Доля жилой площади", 0.1, 1.0, d_living, 0.05)
    norm_housing = st.sidebar.slider("Норма жилья на чел. (м²)", 15, 50, 28)
    st.sidebar.header("💰 Финансовые параметры")
    cost_per_sqm = st.sidebar.slider("Себестоимость (₽/м²)", 50000, 200000, 90000, 5000)
    sale_price = st.sidebar.slider("Цена продажи (₽/м²)", 100000, 700000, 250000, 10000)
    commercial_ratio = st.sidebar.slider("Коэфф. цены коммерции", 0.5, 1.5, 0.85, 0.05)
    land_cost = st.sidebar.slider("Стоимость земли (млн ₽)", 0, 500, 50, 5)

    pzz_config = {
        "min_offset_from_border": offset,
        "max_building_density": density,
        "max_floors": floors,
        "floor_height": floor_height,
        "living_area_ratio": living_ratio,
        "norm_housing_per_person": norm_housing,
        "construction_cost_per_sqm": cost_per_sqm,
        "sale_price_per_sqm": sale_price,
        "commercial_price_ratio": commercial_ratio,
        "land_cost": land_cost * 1000000
    }

    analyzer = UrbanPotentialAnalyzer(parcel_geom, pzz_config, coord_system_detected, restrictions_geom)
    tep = analyzer.calculate_tep()
    financials = analyzer.calculate_financials()
    buildable_geom = analyzer.buildable_geom

    # --- ТЭП ---
    st.header("📊 Технико-экономические показатели")
    if tep['s_total'] == 0:
        st.error("🚨 **Пятно застройки пустое!**")
        st.warning(f"""
        **Причина:** Невозможно построить здание с текущими параметрами.        - Уменьшить отступы от границ (сейчас: {pzz_config['min_offset_from_border']} м)
        - Увеличить плотность застройки (сейчас: {pzz_config['max_building_density']})
        - Проверить ЗОУИТ — возможно, они перекрывают весь участок
        """)
        st.stop()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Площадь участка", f"{tep['s_uch']:,.0f} м²", f"{tep['s_uch_ha']} га")
    col2.metric("Пятно застройки", f"{tep['s_buildable']:,.0f} м²")
    col3.metric("Общая площадь (GBA)", f"{tep['s_total']:,.0f} м²")
    col4.metric("Этажность", f"{tep['floors']} эт", f"{tep['building_height_m']} м")

    st.subheader("🏗️ Структура площадей")
    area_cols = st.columns(3)
    living_pct = safe_percentage(tep['s_living'], tep['s_total'])
    commercial_pct = safe_percentage(tep['s_commercial'], tep['s_total'])
    area_cols[0].info(f"🏠 **Жилая:** {tep['s_living']:,.0f} м² ({living_pct:.1f}%)")
    area_cols[1].info(f"🏢 **Коммерция:** {tep['s_commercial']:,.0f} м² ({commercial_pct:.1f}%)")
    area_cols[2].info(f"🌳 **Озеленение:** {tep['green_space']:,.0f} м²")

    st.subheader("👥 Социальная инфраструктура")
    infra_cols = st.columns(4)
    infra_cols[0].info(f"👪 **Население:** {tep['population']:,} чел")
    infra_cols[1].info(f"🏫 **Школы:** {tep['schools']} мест")
    infra_cols[2].info(f"🧸 **Дет. сады:** {tep['kindergartens']} мест")
    infra_cols[3].info(f"🚗 **Парковка:** {tep['parking']} м/м")

    # --- ФИНАНСЫ ---
    st.header("💰 Финансовая модель")
    fin_cols = st.columns(4)
    fin_cols[0].metric("Выручка (GDV)", f"{financials['total_revenue']/1000000:,.1f} млн ₽")
    fin_cols[1].metric("Затраты", f"{financials['total_cost']/1000000:,.1f} млн ₽")
    profit_color = "normal" if financials['gross_profit'] >= 0 else "inverse"
    fin_cols[2].metric("Прибыль", f"{financials['gross_profit']/1000000:,.1f} млн ₽", delta_color=profit_color)
    fin_cols[3].metric("ROI", f"{financials['roi_percent']}%", delta_color=profit_color)
    with st.expander("📈 Детализация"):
        st.write(f"**Выручка от жилья:** {financials['revenue_living']/1000000:,.1f} млн ₽")
        st.write(f"**Выручка от коммерции:** {financials['revenue_commercial']/1000000:,.1f} млн ₽")
        st.write(f"**Строительство:** {financials['construction_cost']/1000000:,.1f} млн ₽")
        st.write(f"**Инфраструктура:** {financials['infra_cost']/1000000:,.1f} млн ₽")
        st.write(f"**Земля:** {financials['land_cost']/1000000:,.1f} млн ₽")
        st.write(f"**💎 Прибыль с 1 м²:** {financials['profit_per_sqm']:,.0f} ₽")

    # --- ИНСОЛЯЦИЯ ---
    st.header("☀️ Анализ инсоляции")
    building_height = tep['building_height_m']
    latitude = center_lat if coord_system_detected == 'geographic' else 55.75
    shadow_length = calculate_shadow_length(building_height, latitude)
    st.info(f"🏢 **Высота:** {building_height:.1f} м | **Широта:** {latitude:.2f}° | **Тень зимой:** {shadow_length:.1f} м")
    if shadow_length > 50:        st.warning(f"⚠️ Тень достигает {shadow_length:.0f}м — нужна детальная экспертиза")

    # --- КАРТА ---
    st.header("🗺️ Карта территории")
    if coord_system_detected == 'geographic':
        parcel_for_map = parcel_geom
        buildable_for_map = meters_to_wgs84(buildable_geom) if buildable_geom else None
    else:
        st.warning("⚠️ Файл в локальной проекции. Карта схематична.")
        parcel_for_map = parcel_geom
        buildable_for_map = buildable_geom
    try:
        centroid_map = parcel_for_map.centroid
        m = folium.Map(location=[centroid_map.y, centroid_map.x], zoom_start=16, tiles="OpenStreetMap")
        parcel_geojson = {"type": "Feature", "properties": {}, "geometry": mapping(parcel_for_map)}
        folium.GeoJson(
            parcel_geojson, name="Участок",
            style_function=lambda x: {"fillColor": "gray", "fillOpacity": 0.1, "color": "red", "weight": 3}
        ).add_to(m)
        if buildable_for_map and not buildable_for_map.is_empty:
            buildable_geojson = {"type": "Feature", "properties": {}, "geometry": mapping(buildable_for_map)}
            folium.GeoJson(
                buildable_geojson, name="Пятно застройки",
                style_function=lambda x: {"fillColor": "blue", "fillOpacity": 0.4, "color": "blue", "weight": 2}
            ).add_to(m)
        folium.LayerControl().add_to(m)
        st_folium(m, width=1200, height=600)
    except Exception as e:
        st.error(f"Карта недоступна: {e}")

    # --- ЭКСПОРТ ---
    st.header("📥 Экспорт результатов")
    st.subheader("📊 Отчёты")
    exp1 = st.columns(4)
    with exp1[0]:
        full_report = {
            "metadata": {"generated": datetime.now().isoformat(), "coord_system": coord_system_detected},
            "pzz_zone": suggested_pzz['name'],
            "tep": tep, "financials": financials, "pzz_parameters": pzz_config
        }
        st.download_button(
            "📄 JSON-отчёт",
            data=io.BytesIO(json.dumps(full_report, ensure_ascii=False, indent=2).encode()),
            file_name="report.json", mime="application/json"
        )
    with exp1[1]:
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["Показатель", "Значение", "Ед.изм."])
        for k, v in tep.items():            writer.writerow([k, v, ""])
        for k, v in financials.items():
            writer.writerow([k, v, ""])
        st.download_button(
            "📊 ТЭП в Excel (CSV)",
            data=io.BytesIO(csv_buffer.getvalue().encode('utf-8-sig')),
            file_name="tep.csv", mime="text/csv"
        )
    with exp1[2]:
        if buildable_geom and not buildable_geom.is_empty:
            buildable_export = {
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {"area": tep['s_buildable']},
                    "geometry": mapping(buildable_for_map if buildable_for_map else buildable_geom)
                }]
            }
            st.download_button(
                "🗺️ Пятно (GeoJSON)",
                data=io.BytesIO(json.dumps(buildable_export, ensure_ascii=False, indent=2).encode()),
                file_name="buildable.geojson", mime="application/json"
            )
    with exp1[3]:
        obj_content = create_obj_file(buildable_geom, building_height)
        if obj_content:
            st.download_button(
                "🏢 3D OBJ",
                data=io.BytesIO(obj_content.encode()),
                file_name="building.obj", mime="text/plain"
            )

    st.subheader("🏗️ CAD / BIM")
    exp2 = st.columns(2)
    with exp2[0]:
        if EZDXF_AVAILABLE:
            dxf_content = create_dxf_file(
                parcel_geom, buildable_geom, tep, financials,
                building_height, coord_system_detected
            )
            if dxf_content:
                st.download_button(
                    "📐 DXF (AutoCAD, BricsCAD, NanoCAD)",
                    data=dxf_content, file_name="plan.dxf", mime="application/dxf"
                )
                st.caption("Со слоями: PARCEL, BUILDABLE, BUILDING_3D, TEXT")
            else:
                st.warning("Ошибка генерации DXF")
        else:
            st.warning("Библиотека ezdxf не установлена")
    with exp2[1]:
        st.info("💡 **IFC экспорт:** Установите `ifcopenshell` локально для BIM-интеграции с Revit/ArchiCAD")
        st.caption("В веб-версии IFC недоступен из-за тяжёлых зависимостей. Используйте DXF.")

    st.success("✅ Анализ завершён!")

else:
    st.warning("⚠️ Загрузите файл для начала анализа")

st.markdown("---")
st.caption("🛠️ Urban Potential Analyzer PRO v2.1 | Universal coordinate system | Auto PZZ detection")