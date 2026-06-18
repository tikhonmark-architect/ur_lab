import streamlit as st
import folium
from streamlit_folium import st_folium
from shapely.geometry import Polygon, shape, mapping, MultiPolygon
from shapely.ops import transform, unary_union
import pyproj
import math
import json
import io
import csv
from datetime import datetime

# ============================================================================
# БЕЗОПАСНЫЕ ИМПОРТЫ ОПЦИОНАЛЬНЫХ БИБЛИОТЕК
# ============================================================================

DXF_AVAILABLE = False
SHP_AVAILABLE = False

try:
    import ezdxf
    DXF_AVAILABLE = True
except ImportError:
    pass

try:
    import shapefile as shp
    import zipfile
    import tempfile
    import os
    SHP_AVAILABLE = True
except ImportError:
    pass

# ============================================================================
# ФУНКЦИИ ПАРСИНГА ФАЙЛОВ
# ============================================================================

def parse_geojson(file_content):
    """Парсинг GeoJSON файла"""
    try:
        geojson_data = json.loads(file_content.decode("utf-8"))
        
        if geojson_data['type'] == 'FeatureCollection':
            geoms = [shape(f['geometry']) for f in geojson_data['features']]
            return unary_union(geoms)
        elif geojson_data['type'] == 'Feature':
            return shape(geojson_data['geometry'])
        else:
            return shape(geojson_data)
    except Exception as e:
        raise ValueError(f"Ошибка парсинга GeoJSON: {str(e)}")

def parse_dxf(file_content):
    """Парсинг DXF файла (AutoCAD)"""
    if not DXF_AVAILABLE:
        raise ValueError("Библиотека ezdxf не установлена. Используйте GeoJSON формат.")
    
    try:
        doc = ezdxf.read(io.BytesIO(file_content))
        msp = doc.modelspace()
        
        polygons = []
        
        for entity in msp:
            if entity.dxftype() == 'LWPOLYLINE':
                if entity.closed:
                    points = list(entity.get_points(format='xy'))
                    if len(points) >= 3:
                        polygons.append(Polygon(points))
            
            elif entity.dxftype() == 'POLYLINE':
                if entity.is_closed:
                    points = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
                    if len(points) >= 3:
                        polygons.append(Polygon(points))
        
        if not polygons:
            raise ValueError("В DXF файле не найдено замкнутых полилиний.")
        
        return unary_union(polygons)
        
    except Exception as e:
        raise ValueError(f"Ошибка парсинга DXF: {str(e)}")

def parse_shapefile(zip_content):
    """Парсинг Shapefile из ZIP-архива"""
    if not SHP_AVAILABLE:
        raise ValueError("Библиотека pyshp не установлена. Используйте GeoJSON формат.")
    
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = os.path.join(tmpdir, 'shapefile.zip')
            with open(zip_path, 'wb') as f:
                f.write(zip_content)
            
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(tmpdir)
            
            shp_files = [f for f in os.listdir(tmpdir) if f.endswith('.shp')]
            if not shp_files:
                raise ValueError("В ZIP-архиве не найден файл .shp")
            
            shp_path = os.path.join(tmpdir, shp_files[0].replace('.shp', ''))
            sf = shp.Reader(shp_path)
            shapes = sf.shapes()
            
            if not shapes:
                raise ValueError("Shapefile не содержит геометрии")
            
            polygons = []
            for shape_record in shapes:
                if shape_record.shapeType in [5, 15, 25]:
                    points = shape_record.points
                    if len(points) >= 3:
                        polygons.append(Polygon(points))
            
            if not polygons:
                raise ValueError("В Shapefile не найдено полигонов")
            
            return unary_union(polygons)
            
    except Exception as e:
        raise ValueError(f"Ошибка парсинга Shapefile: {str(e)}")

# ============================================================================
# ФУНКЦИИ КОНВЕРТАЦИИ КООРДИНАТ
# ============================================================================

def wgs84_to_meters(geom):
    """Конвертация из WGS84 в Web Mercator"""
    project = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    return transform(project.transform, geom)

def meters_to_wgs84(geom):
    """Конвертация из Web Mercator в WGS84"""
    project = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    return transform(project.transform, geom)

def calculate_geodesic_area(geom_wgs84):
    """Точный геодезический расчет площади"""
    if geom_wgs84 is None or geom_wgs84.is_empty:
        return 0
    
    geod = pyproj.Geod(ellps="WGS84")
    
    if geom_wgs84.geom_type == 'Polygon':
        exterior_coords = list(geom_wgs84.exterior.coords)
        lons = [c[0] for c in exterior_coords]
        lats = [c[1] for c in exterior_coords]
        _, area = geod.polygon_area_perimeter(lons, lats)
        return abs(area)
        
    elif geom_wgs84.geom_type == 'MultiPolygon':
        total_area = 0
        for polygon in geom_wgs84.geoms:
            exterior_coords = list(polygon.exterior.coords)
            lons = [c[0] for c in exterior_coords]
            lats = [c[1] for c in exterior_coords]
            _, area = geod.polygon_area_perimeter(lons, lats)
            total_area += abs(area)
        return total_area
    
    return 0

def detect_coordinate_system(geom):
    """Определяет систему координат по размеру bounding box"""
    bounds = geom.bounds
    minx, miny, maxx, maxy = bounds
    
    if -180 <= minx <= 180 and -90 <= miny <= 90 and -180 <= maxx <= 180 and -90 <= maxy <= 90:
        return "WGS84"
    else:
        return "METERS"

# ============================================================================
# ФУНКЦИИ ДЛЯ 3D ЭКСПОРТА И ИНСОЛЯЦИИ
# ============================================================================

def create_obj_file(geometry_meters, height_meters):
    """Создание OBJ файла из 2D полигона с экструзией"""
    if geometry_meters is None or geometry_meters.is_empty:
        return ""
    
    coords = []
    if geometry_meters.geom_type == 'Polygon':
        coords = list(geometry_meters.exterior.coords)
    elif geometry_meters.geom_type == 'MultiPolygon':
        coords = list(geometry_meters.geoms[0].exterior.coords)
    else:
        return ""
    
    if len(coords) < 3:
        return ""
    
    min_x = min(c[0] for c in coords)
    min_y = min(c[1] for c in coords)
    
    obj_content = []
    obj_content.append("# Building 3D Model")
    obj_content.append(f"# Height: {height_meters:.1f}m")
    obj_content.append("")
    
    for x, y in coords[:-1]:
        obj_content.append(f"v {x - min_x:.2f} {y - min_y:.2f} 0.0")
    
    for x, y in coords[:-1]:
        obj_content.append(f"v {x - min_x:.2f} {y - min_y:.2f} {height_meters:.2f}")
    
    obj_content.append("")
    
    n = len(coords) - 1
    if n < 3:
        return ""
    
    base_face = "f " + " ".join(str(i+1) for i in range(n))
    obj_content.append(base_face)
    
    roof_face = "f " + " ".join(str(i+1+n) for i in range(n))
    obj_content.append(roof_face)
    
    for i in range(n):
        next_i = (i + 1) % n
        side_face = f"f {i+1} {next_i+1} {next_i+1+n} {i+1+n}"
        obj_content.append(side_face)
    
    return "\n".join(obj_content)

def calculate_shadow_length(building_height, latitude=55.75):
    """Расчет длины тени"""
    solar_angle = 90 - abs(latitude + 23.45)
    solar_angle_rad = math.radians(max(solar_angle, 5))
    shadow_length = building_height / math.tan(solar_angle_rad)
    return shadow_length

# ============================================================================
# КЛАСС АНАЛИЗАТОРА
# ============================================================================

class UrbanPotentialAnalyzer:
    def __init__(self, parcel_geom_meters, parcel_geom_wgs84, pzz_config, restrictions_geom=None):
        self.parcel_geom = parcel_geom_meters
        self.parcel_geom_wgs84 = parcel_geom_wgs84
        self.pzz = pzz_config
        self.restrictions = restrictions_geom
        self.buildable_geom = None
        self.buildable_geom_wgs84 = None
        self.tep = {}
        self.financials = {}
        self.real_area = calculate_geodesic_area(parcel_geom_wgs84)

    def calculate_buildable_area(self):
        min_offset = self.pzz.get("min_offset_from_border", 0)
        buildable = self.parcel_geom.buffer(-min_offset)
        
        if self.restrictions is not None and not self.restrictions.is_empty:
            buildable = buildable.difference(self.restrictions)
        
        self.buildable_geom = buildable
        
        if buildable and not buildable.is_empty:
            self.buildable_geom_wgs84 = meters_to_wgs84(buildable)
            self.buildable_real_area = calculate_geodesic_area(self.buildable_geom_wgs84)
        else:
            self.buildable_geom_wgs84 = None
            self.buildable_real_area = 0
            
        return buildable

    def calculate_tep(self):
        if self.buildable_geom is None:
            self.calculate_buildable_area()
        
        s_uch = self.real_area
        s_buildable = self.buildable_real_area
        
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
            "s_uch_ha": round(s_uch / 10000, 2),
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
            "parking": math.ceil((s_total / 100) * 1.2),
            "green_space": math.ceil(population * 6)
        }
        return self.tep

    def calculate_financials(self):
        if not self.tep:
            self.calculate_tep()
        
        cost_per_sqm = self.pzz.get("construction_cost_per_sqm", 80000)
        sale_price_per_sqm = self.pzz.get("sale_price_per_sqm", 200000)
        commercial_price_ratio = self.pzz.get("commercial_price_ratio", 0.8)
        land_cost = self.pzz.get("land_cost", 50000000)
        
        s_living = self.tep["s_living"]
        s_commercial = self.tep["s_commercial"]
        
        revenue_living = s_living * sale_price_per_sqm
        revenue_commercial = s_commercial * sale_price_per_sqm * commercial_price_ratio
        total_revenue = revenue_living + revenue_commercial
        
        construction_cost = self.tep["s_total"] * cost_per_sqm
        infra_cost = (self.tep["schools"] * 1500000 + 
                      self.tep["kindergartens"] * 1200000 + 
                      self.tep["parking"] * 800000)
        total_cost = construction_cost + land_cost + infra_cost
        
        gross_profit = total_revenue - total_cost
        roi = (gross_profit / total_cost * 100) if total_cost > 0 else 0
        
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
            "profit_per_sqm": round(gross_profit / self.tep["s_total"], 0) if self.tep["s_total"] > 0 else 0
        }
        return self.financials

# ============================================================================
# STREAMLIT ИНТЕРФЕЙС
# ============================================================================

st.set_page_config(page_title="Градостроительный Потенциал PRO", layout="wide")
st.title("🏙️ Анализатор градостроительного потенциала PRO")
st.markdown("**Полный комплексный анализ:** Точный расчет ТЭП + Финансы + 3D + Инсоляция + ЗОУИТ")

# ============================================================================
# ЗАГРУЗКА ДАННЫХ
# ============================================================================

st.header("📂 Загрузка территории")

data_source = st.radio(
    "Выберите источник данных:",
    ["Тестовый участок (пример)", "Загрузить свой файл"],
    horizontal=True
)

parcel_geom_meters = None
parcel_geom_wgs84 = None
restrictions_geom_meters = None

if data_source == "Загрузить свой файл":
    # Показываем доступные форматы
    available_formats = ["GeoJSON (.geojson, .json)"]
    if DXF_AVAILABLE:
        available_formats.append("AutoCAD DXF (.dxf)")
    if SHP_AVAILABLE:
        available_formats.append("Shapefile (.zip)")
    
    st.info(f"""
     **Поддерживаемые форматы:** {', '.join(available_formats)}
    
    ✅ **Рекомендуется GeoJSON** - работает на всех платформах.
    
     **Как конвертировать в GeoJSON:**
    - **AutoCAD:** File → Save As → DXF, затем откройте в QGIS и экспортируйте в GeoJSON
    - **Shapefile:** Откройте в QGIS → Export → Save Features As → GeoJSON
    """)
    
    # Выбор формата
    file_format = st.selectbox(
        "Выберите формат файла:",
        available_formats,
        help="GeoJSON рекомендуется для стабильной работы"
    )
    
    # Загрузка файла в зависимости от формата
    uploaded_file = None
    
    if file_format == "GeoJSON (.geojson, .json)":
        uploaded_file = st.file_uploader(
            "1️⃣ Загрузите GeoJSON файл с границами участка",
            type=['geojson', 'json'],
            key="main_parcel"
        )
    elif file_format == "AutoCAD DXF (.dxf)" and DXF_AVAILABLE:
        uploaded_file = st.file_uploader(
            "1️⃣ Загрузите DXF файл (экспорт из AutoCAD)",
            type=['dxf'],
            key="main_parcel"
        )
    elif file_format == "Shapefile (.zip)" and SHP_AVAILABLE:
        uploaded_file = st.file_uploader(
            "1️ Загрузите ZIP-архив с Shapefile (.shp, .shx, .dbf)",
            type=['zip'],
            key="main_parcel"
        )
    
    # Ограничения (только GeoJSON)
    uploaded_restrictions = st.file_uploader(
        "2️ Загрузите GeoJSON с ограничениями (ЗОУИТ) - *опционально*",
        type=['geojson', 'json'],
        key="restrictions"
    )
    
    if uploaded_file is not None:
        try:
            file_content = uploaded_file.getvalue()
            
            # Парсинг в зависимости от формата
            if file_format == "GeoJSON (.geojson, .json)":
                geom = parse_geojson(file_content)
            elif file_format == "AutoCAD DXF (.dxf)":
                geom = parse_dxf(file_content)
            else:
                geom = parse_shapefile(file_content)
            
            if geom is None or geom.is_empty:
                st.error("❌ Не удалось извлечь геометрию из файла")
            else:
                # Определяем систему координат
                coord_system = detect_coordinate_system(geom)
                
                if coord_system == "WGS84":
                    parcel_geom_wgs84 = geom
                    parcel_geom_meters = wgs84_to_meters(geom)
                    st.success(f"✅ Файл загружен! **Система координат:** WGS84 (градусы)")
                else:
                    st.warning("""
                    ⚠️ **Обнаружены координаты в метрах** (локальная система координат).
                    
                    Для корректного отображения на карте рекомендуется использовать **GeoJSON** 
                    с системой координат **WGS84 (EPSG:4326)**.
                    """)
                    parcel_geom_meters = geom
                    parcel_geom_wgs84 = meters_to_wgs84(geom)
                
                # Показываем площадь
                real_area = calculate_geodesic_area(parcel_geom_wgs84)
                st.success(f"**Реальная площадь: {real_area:,.0f} м² ({real_area/10000:.2f} га)**")
                
                # Загрузка ограничений
                if uploaded_restrictions is not None:
                    try:
                        restrictions_content = uploaded_restrictions.getvalue()
                        restrictions_geom_wgs84 = parse_geojson(restrictions_content)
                        restrictions_geom_meters = wgs84_to_meters(restrictions_geom_wgs84)
                        restrictions_area = calculate_geodesic_area(restrictions_geom_wgs84)
                        st.info(f"🚫 Загружены ограничения: **{restrictions_area:,.0f} м²** ({restrictions_area/10000:.2f} га)")
                    except Exception as e:
                        st.warning(f"⚠️ Не удалось загрузить ограничения: {str(e)}")
            
        except Exception as e:
            st.error(f"❌ Ошибка при чтении файла: {str(e)}")
            st.info("Убедитесь, что файл в корректном формате и содержит валидную геометрию.")

else:
    st.info("Используется демонстрационный участок в центре Москвы")
    coords_wgs84 = [
        (37.61500, 55.75200),
        (37.61750, 55.75200),
        (37.61750, 55.75350),
        (37.61500, 55.75350)
    ]
    
    parcel_geom_wgs84 = Polygon(coords_wgs84)
    parcel_geom_meters = wgs84_to_meters(parcel_geom_wgs84)

# ============================================================================
# ПАРАМЕТРЫ ПЗЗ И ФИНАНСОВ
# ============================================================================

if parcel_geom_meters is not None:
    st.sidebar.header("⚙️ Градостроительные параметры")
    offset = st.sidebar.slider("Мин. отступ от границ (м)", 0, 30, 5)
    density = st.sidebar.slider("Макс. плотность застройки", 0.1, 1.0, 0.4, 0.05)
    floors = st.sidebar.slider("Предельная этажность", 1, 50, 9)
    floor_height = st.sidebar.slider("Высота этажа (м)", 2.8, 4.5, 3.2, 0.1)
    living_ratio = st.sidebar.slider("Доля жилой площади", 0.1, 1.0, 0.75, 0.05)
    norm_housing = st.sidebar.slider("Норма жилья на чел. (кв.м)", 15, 50, 28)
    
    st.sidebar.header("💰 Финансовые параметры")
    cost_per_sqm = st.sidebar.slider("Себестоимость (₽/м²)", 50000, 200000, 90000, 5000)
    sale_price = st.sidebar.slider("Цена продажи жилья (₽/м²)", 100000, 700000, 250000, 10000)
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

    analyzer = UrbanPotentialAnalyzer(parcel_geom_meters, parcel_geom_wgs84, pzz_config, restrictions_geom_meters)
    tep = analyzer.calculate_tep()
    financials = analyzer.calculate_financials()
    buildable_geom_meters = analyzer.buildable_geom
    buildable_geom_wgs84 = analyzer.buildable_geom_wgs84

    # ========================================================================
    # ОТОБРАЖЕНИЕ ТЭП
    # ========================================================================
    
    st.header(" Технико-экономические показатели")
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Площадь участка", f"{tep['s_uch']:,.0f} м²", f"{tep['s_uch_ha']} га")
    col2.metric("Пятно застройки", f"{tep['s_buildable']:,.0f} м²")
    col3.metric("Общая площадь (GBA)", f"{tep['s_total']:,.0f} м²")
    col4.metric("Этажность", f"{tep['floors']} эт", f"{tep['building_height_m']} м")

    st.subheader("🏗️ Структура площадей")
    area_cols = st.columns(3)
    area_cols[0].info(f"🏠 **Жилая:** {tep['s_living']:,.0f} м² ({tep['s_living']/tep['s_total']*100:.1f}%)")
    area_cols[1].info(f"🏢 **Коммерция:** {tep['s_commercial']:,.0f} м² ({tep['s_commercial']/tep['s_total']*100:.1f}%)")
    area_cols[2].info(f"🌳 **Озеленение:** {tep['green_space']:,.0f} м²")

    st.subheader("👥 Социальная инфраструктура")
    infra_cols = st.columns(4)
    infra_cols[0].info(f" **Население:** {tep['population']:,} чел")
    infra_cols[1].info(f"🏫 **Школы:** {tep['schools']} мест")
    infra_cols[2].info(f"🧸 **Дет. сады:** {tep['kindergartens']} мест")
    infra_cols[3].info(f"🚗 **Парковка:** {tep['parking']} м/м")

    # ========================================================================
    # ФИНАНСОВЫЕ ПОКАЗАТЕЛИ
    # ========================================================================
    
    st.header("💰 Финансовая модель")
    
    fin_cols = st.columns(4)
    fin_cols[0].metric("Выручка (GDV)", f"{financials['total_revenue']/1000000:,.1f} млн ₽")
    fin_cols[1].metric("Затраты", f"{financials['total_cost']/1000000:,.1f} млн ₽")
    
    profit_color = "normal" if financials['gross_profit'] >= 0 else "inverse"
    fin_cols[2].metric("Валовая прибыль", f"{financials['gross_profit']/1000000:,.1f} млн ₽", delta_color=profit_color)
    fin_cols[3].metric("Рентабельность (ROI)", f"{financials['roi_percent']}%", delta_color=profit_color)

    with st.expander("📈 Детализация финансового расчета"):
        st.write(f"**Выручка от жилья:** {financials['revenue_living']/1000000:,.1f} млн ₽")
        st.write(f"**Выручка от коммерции:** {financials['revenue_commercial']/1000000:,.1f} млн ₽")
        st.write("---")
        st.write(f"**Затраты на строительство:** {financials['construction_cost']/1000000:,.1f} млн ₽")
        st.write(f"**Затраты на инфраструктуру:** {financials['infra_cost']/1000000:,.1f} млн ₽")
        st.write(f"**Стоимость земли:** {financials['land_cost']/1000000:,.1f} млн ₽")
        st.write("---")
        st.write(f"**💎 Прибыль с 1 м²:** {financials['profit_per_sqm']:,.0f} ₽")

    # ========================================================================
    # АНАЛИЗ ИНСОЛЯЦИИ
    # ========================================================================
    
    st.header("☀️ Анализ инсоляции")
    building_height = tep['building_height_m']
    centroid_wgs84 = parcel_geom_wgs84.centroid
    latitude = centroid_wgs84.y
    shadow_length = calculate_shadow_length(building_height, latitude)
    
    st.info(f"🏢 **Высота здания:** {building_height:.1f} м | **Широта:** {latitude:.2f}° | **Длина тени:** {shadow_length:.1f} м")
    
    if shadow_length > 50:
        st.warning(f"⚠️ Тень достигает **{shadow_length:.0f}м**. Рекомендуется детальная инсоляционная экспертиза!")
    
    if building_height > 75:
        st.error(f"🚨 Здание выше 75м требует обязательной разработки СТУ")

    # ========================================================================
    # КАРТА
    # ========================================================================
    
    st.header("🗺️ Карта территории и строительного пятна")
    
    center_lat = centroid_wgs84.y
    center_lon = centroid_wgs84.x
    
    m = folium.Map(location=[center_lat, center_lon], zoom_start=16, tiles="OpenStreetMap")

    parcel_geojson = {"type": "Feature", "properties": {"name": "Участок", "area_m2": tep['s_uch']}, "geometry": mapping(parcel_geom_wgs84)}
    folium.GeoJson(
        parcel_geojson,
        name="Границы участка",
        style_function=lambda x: {"fillColor": "gray", "fillOpacity": 0.1, "color": "red", "weight": 3},
        tooltip=folium.GeoJsonTooltip(fields=["name", "area_m2"], aliases=["Объект:", "Площадь:"])
    ).add_to(m)

    if buildable_geom_wgs84 is not None and not buildable_geom_wgs84.is_empty:
        buildable_geojson = {"type": "Feature", "properties": {"name": "Пятно застройки", "area_m2": tep['s_buildable']}, "geometry": mapping(buildable_geom_wgs84)}
        folium.GeoJson(
            buildable_geojson,
            name="Пятно застройки",
            style_function=lambda x: {"fillColor": "blue", "fillOpacity": 0.4, "color": "blue", "weight": 2},
            tooltip=folium.GeoJsonTooltip(fields=["name", "area_m2"], aliases=["Объект:", "Площадь:"])
        ).add_to(m)

    if shadow_length > 0 and buildable_geom_meters is not None:
        try:
            shadow_zone_meters = buildable_geom_meters.buffer(shadow_length)
            shadow_zone_wgs84 = meters_to_wgs84(shadow_zone_meters)
            shadow_geojson = {"type": "Feature", "properties": {"name": f"Зона тени ({shadow_length:.0f}м)"}, "geometry": mapping(shadow_zone_wgs84)}
            
            folium.GeoJson(
                shadow_geojson,
                name=f"Зона тени ({shadow_length:.0f}м)",
                style_function=lambda x: {"fillColor": "orange", "fillOpacity": 0.15, "color": "orange", "weight": 1, "dashArray": "5, 5"}
            ).add_to(m)
        except Exception as e:
            pass

    if restrictions_geom_meters is not None:
        try:
            restrictions_wgs84 = meters_to_wgs84(restrictions_geom_meters)
            restrictions_geojson = {"type": "Feature", "properties": {"name": "Ограничения (ЗОУИТ)"}, "geometry": mapping(restrictions_wgs84)}
            folium.GeoJson(
                restrictions_geojson,
                name="Ограничения (ЗОУИТ)",
                style_function=lambda x: {"fillColor": "red", "fillOpacity": 0.3, "color": "darkred", "weight": 2, "dashArray": "3, 3"}
            ).add_to(m)
        except Exception as e:
            pass

    folium.LayerControl().add_to(m)
    st_folium(m, width=1200, height=600)

    # ========================================================================
    # ЭКСПОРТ ДАННЫХ
    # ========================================================================
    
    st.header(" Экспорт результатов")
    
    export_cols = st.columns(4)
    
    with export_cols[0]:
        full_report = {
            "metadata": {
                "generated": datetime.now().isoformat(),
                "latitude": latitude,
                "longitude": center_lon
            },
            "tep": tep,
            "financials": financials,
            "pzz_parameters": pzz_config
        }
        st.download_button(
            label="📊 Полный отчет (JSON)",
            data=io.BytesIO(json.dumps(full_report, ensure_ascii=False, indent=2).encode()),
            file_name="urban_analysis_report.json",
            mime="application/json"
        )
    
    with export_cols[1]:
        if buildable_geom_wgs84 is not None:
            buildable_export = {
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {
                        "name": "Пятно застройки",
                        "area_m2": tep['s_buildable'],
                        "area_ha": tep['s_buildable'] / 10000
                    },
                    "geometry": mapping(buildable_geom_wgs84)
                }]
            }
            st.download_button(
                label="🗺️ Пятно застройки (GeoJSON)",
                data=io.BytesIO(json.dumps(buildable_export, ensure_ascii=False, indent=2).encode()),
                file_name="buildable_area.geojson",
                mime="application/json"
            )
    
    with export_cols[2]:
        obj_content = create_obj_file(buildable_geom_meters, building_height)
        if obj_content:
            st.download_button(
                label=" 3D модель (OBJ)",
                data=io.BytesIO(obj_content.encode()),
                file_name="building_3d.obj",
                mime="text/plain"
            )
        else:
            st.warning("3D недоступна")
    
    with export_cols[3]:
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["Показатель", "Значение", "Ед.изм."])
        writer.writerow(["Площадь участка", tep['s_uch'], "м²"])
        writer.writerow(["Площадь участка", tep['s_uch_ha'], "га"])
        writer.writerow(["Пятно застройки", tep['s_buildable'], "м²"])
        writer.writerow(["Общая площадь", tep['s_total'], "м²"])
        writer.writerow(["Жилая площадь", tep['s_living'], "м²"])
        writer.writerow(["Коммерческая площадь", tep['s_commercial'], "м²"])
        writer.writerow(["Этажность", tep['floors'], "эт"])
        writer.writerow(["Высота здания", tep['building_height_m'], "м"])
        writer.writerow(["Население", tep['population'], "чел"])
        writer.writerow(["Школы", tep['schools'], "мест"])
        writer.writerow(["Детские сады", tep['kindergartens'], "мест"])
        writer.writerow(["Парковка", tep['parking'], "м/м"])
        writer.writerow(["Выручка", financials['total_revenue'], "₽"])
        writer.writerow(["Затраты", financials['total_cost'], "₽"])
        writer.writerow(["Прибыль", financials['gross_profit'], "₽"])
        writer.writerow(["ROI", financials['roi_percent'], "%"])
        
        st.download_button(
            label="📄 ТЭП в Excel (CSV)",
            data=io.BytesIO(csv_buffer.getvalue().encode('utf-8-sig')),
            file_name="tep_report.csv",
            mime="text/csv"
        )

    st.success("✅ Полный анализ завершен! Все данные готовы к экспорту.")

else:
    st.warning("⚠️ Загрузите файл с границами участка или выберите тестовые данные для начала анализа.")

st.markdown("---")
st.caption("️ Urban Potential Analyzer PRO | Точный геодезический расчет на эллипсоиде WGS84")