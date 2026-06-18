import streamlit as st
import folium
from streamlit_folium import st_folium
from shapely.geometry import Polygon, shape, mapping, MultiPolygon
from shapely.ops import transform, unary_union
import pyproj
import math
import json
import io
import numpy as np
from datetime import datetime

# ============================================================================
# ФУНКЦИИ КОНВЕРТАЦИИ КООРДИНАТ
# ============================================================================

def wgs84_to_meters(geom):
    """Конвертация из WGS84 (градусы) в Web Mercator (метры)"""
    project = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    return transform(project.transform, geom)

def meters_to_wgs84(geom):
    """Конвертация из Web Mercator (метры) в WGS84 (градусы)"""
    project = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    return transform(project.transform, geom)

# ============================================================================
# ФУНКЦИИ ДЛЯ 3D ЭКСПОРТА
# ============================================================================

def create_obj_file(geometry, height_meters):
    """Создание OBJ 3D модели из полигона"""
    if geometry is None or geometry.is_empty:
        return ""
    
    # Получаем координаты полигона
    coords = []
    if geometry.geom_type == 'Polygon':
        coords = list(geometry.exterior.coords)
    elif geometry.geom_type == 'MultiPolygon':
        coords = list(geometry.geoms[0].exterior.coords)
    
    if len(coords) < 3:
        return ""
    
    # Находим минимальные координаты для центрирования
    min_x = min(c[0] for c in coords)
    min_y = min(c[1] for c in coords)
    
    lines = []
    lines.append("# Urban Potential Analyzer - 3D Building")
    lines.append(f"# Height: {height_meters:.1f}m")
    lines.append("")
    
    # Вершины основания (z = 0)
    for x, y in coords[:-1]:
        lines.append(f"v {x - min_x:.2f} {y - min_y:.2f} 0.0")
    
    # Вершины крыши (z = height)
    for x, y in coords[:-1]:
        lines.append(f"v {x - min_x:.2f} {y - min_y:.2f} {height_meters:.2f}")
    
    lines.append("")
    
    n = len(coords) - 1
    if n < 3:
        return ""
    
    # Основание (нижняя грань)
    base_face = "f " + " ".join(str(i + 1) for i in range(n))
    lines.append(base_face)
    
    # Крыша (верхняя грань)
    roof_face = "f " + " ".join(str(i + 1 + n) for i in range(n))
    lines.append(roof_face)
    
    # Боковые стены
    for i in range(n):
        next_i = (i + 1) % n
        side_face = f"f {i + 1} {next_i + 1} {next_i + 1 + n} {i + 1 + n}"
        lines.append(side_face)
    
    return "\n".join(lines)

# ============================================================================
# ФУНКЦИИ ДЛЯ ИНСОЛЯЦИИ (УПРОЩЕННЫЕ)
# ============================================================================

def calculate_shadow_length(building_height, latitude=55.75, date=None):
    """
    Упрощенный расчет длины тени от здания.
    latitude: широта (по умолчанию Москва)
    """
    if date is None:
        date = datetime(2024, 12, 21)  # Зимнее солнцестояние (худший случай)
    
    # Упрощенная формула: длина тени = высота / tan(угол_солнца)
    # Для зимнего солнцестояния в Москве угол солнца ~11°
    # Для летнего ~57°
    
    day_of_year = date.timetuple().tm_yday
    
    # Приблизительный угол солнца в полдень    solar_angle = 90 - abs(latitude - 23.45 * math.sin(math.radians((day_of_year - 81) * 360 / 365)))
    solar_angle_rad = math.radians(max(solar_angle, 5))  # Минимум 5 градусов
    
    shadow_length = building_height / math.tan(solar_angle_rad)
    return shadow_length

def check_shadow_overlap(building_geom, shadow_length, neighbor_geom):
    """Проверка, падает ли тень на соседнее здание"""
    # Упрощенно: создаем буфер вокруг здания на длину тени
    shadow_zone = building_geom.buffer(shadow_length)
    
    # Проверяем пересечение
    overlap = shadow_zone.intersection(neighbor_geom)
    overlap_area = overlap.area if not overlap.is_empty else 0
    
    return overlap_area > 0, overlap_area

# ============================================================================
# КЛАСС АНАЛИЗАТОРА (ОБНОВЛЕННЫЙ)
# ============================================================================

class UrbanPotentialAnalyzer:
    def __init__(self, parcel_geom, pzz_config, coord_system='auto', restrictions_geom=None):
        self.parcel_geom = parcel_geom
        self.pzz = pzz_config
        self.restrictions = restrictions_geom
        self.coord_system = coord_system if coord_system != 'auto' else detect_coordinate_system(parcel_geom)
        self.buildable_geom = None
        self.tep = {}
        self.financials = {}
        
        # КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: корректный расчёт площади
        self.real_area, _ = calculate_area_universal(parcel_geom)

    def calculate_buildable_area(self):
        min_offset = self.pzz.get("min_offset_from_border", 0)
        
        # Если координаты в градусах — конвертируем в метры для buffer()
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
        
        self.buildable_geom = buildable
        self.buildable_area, _ = calculate_area_universal(buildable)
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
st.markdown("**Полный комплексный анализ:** ТЭП + Финансы + 3D + Инсоляция")

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
    st.info("💡 Поддерживаемый формат: **GeoJSON** (.geojson, .json)")
        # Основной участок
    uploaded_file = st.file_uploader(
        "1️⃣ Загрузите GeoJSON файл с границами участка",
        type=['geojson', 'json'],
        key="main_parcel"
    )
    
    # Ограничения (ЗОУИТ)
    uploaded_restrictions = st.file_uploader(
        "2️⃣ Загрузите GeoJSON с ограничениями (ЗОУИТ, красные линии) - *опционально*",
        type=['geojson', 'json'],
        key="restrictions"
    )
    
    if uploaded_file is not None:
        try:
            geojson_data = json.loads(uploaded_file.getvalue().decode("utf-8"))
            
            if geojson_data['type'] == 'FeatureCollection':
                geom_dict = geojson_data['features'][0]['geometry']
            elif geojson_data['type'] == 'Feature':
                geom_dict = geojson_data['geometry']
            else:
                geom_dict = geojson_data
            
            parcel_geom_wgs84 = shape(geom_dict)
            parcel_geom_meters = wgs84_to_meters(parcel_geom_wgs84)
            
            st.success(f"✅ Участок загружен! Площадь: {parcel_geom_meters.area:,.0f} м²")
            
            # Загрузка ограничений
            if uploaded_restrictions is not None:
                try:
                    restrictions_data = json.loads(uploaded_restrictions.getvalue().decode("utf-8"))
                    
                    if restrictions_data['type'] == 'FeatureCollection':
                        # Объединяем все полигоны ограничений
                        geoms = [shape(f['geometry']) for f in restrictions_data['features']]
                        restrictions_wgs84 = unary_union(geoms)
                    elif restrictions_data['type'] == 'Feature':
                        restrictions_wgs84 = shape(restrictions_data['geometry'])
                    else:
                        restrictions_wgs84 = shape(restrictions_data)
                    
                    restrictions_geom_meters = wgs84_to_meters(restrictions_wgs84)
                    st.info(f"🚫 Загружены ограничения: {restrictions_geom_meters.area:,.0f} м²")
                    
                except Exception as e:
                    st.warning(f"⚠️ Не удалось загрузить ограничения: {str(e)}")
                    except Exception as e:
            st.error(f"❌ Ошибка: {str(e)}")

else:
    st.info("Используется демонстрационный участок")
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
    living_ratio = st.sidebar.slider("Доля жилой площади", 0.1, 1.0, 0.75, 0.05)
    norm_housing = st.sidebar.slider("Норма жилья на чел. (кв.м)", 15, 50, 25)
    
    st.sidebar.header("💰 Финансовые параметры")
    cost_per_sqm = st.sidebar.slider("Себестоимость строительства (₽/м²)", 50000, 150000, 80000, 5000)
    sale_price = st.sidebar.slider("Цена продажи (₽/м²)", 100000, 500000, 200000, 10000)
    land_cost = st.sidebar.slider("Стоимость земли (₽)", 10000000, 200000000, 50000000, 5000000)

    pzz_config = {
        "min_offset_from_border": offset,
        "max_building_density": density,
        "max_floors": floors,
        "living_area_ratio": living_ratio,
        "norm_housing_per_person": norm_housing,
        "construction_cost_per_sqm": cost_per_sqm,
        "sale_price_per_sqm": sale_price,
        "land_cost": land_cost
    }

    # Запуск анализатора
    analyzer = UrbanPotentialAnalyzer(parcel_geom_meters, pzz_config, restrictions_geom_meters)
    tep = analyzer.calculate_tep()
    financials = analyzer.calculate_financials()
    buildable_geom_meters = analyzer.buildable_geom

    # ========================================================================    # ОТОБРАЖЕНИЕ ТЭП
    # ========================================================================
    
    st.header("📊 Технико-экономические показатели")
    cols = st.columns(4)
    cols[0].metric("Общая площадь (GBA)", f"{tep['s_total']:,.0f} м²")
    cols[1].metric("Жилая площадь", f"{tep['s_living']:,.0f} м²")
    cols[2].metric("Коммерческая площадь", f"{tep['s_commercial']:,.0f} м²")
    cols[3].metric("Этажность", f"{tep['floors']} эт")

    st.subheader("👥 Социальная инфраструктура")
    infra_cols = st.columns(3)
    infra_cols[0].info(f"🏫 Школы: **{tep['schools']}** мест")
    infra_cols[1].info(f"🧸 Детские сады: **{tep['kindergartens']}** мест")
    infra_cols[2].info(f"🚗 Парковка: **{tep['parking']}** м/м")

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

    # Детализация
    with st.expander("📈 Детализация финансового расчета"):
        st.write(f"**Выручка от жилья:** {financials['revenue_living']/1000000:,.1f} млн ₽")
        st.write(f"**Выручка от коммерции:** {financials['revenue_commercial']/1000000:,.1f} млн ₽")
        st.write(f"**Затраты на строительство:** {financials['construction_cost']/1000000:,.1f} млн ₽")
        st.write(f"**Стоимость земли:** {financials['land_cost']/1000000:,.1f} млн ₽")

    # ========================================================================
    # АНАЛИЗ ИНСОЛЯЦИИ
    # ========================================================================
    
    st.header("☀️ Анализ инсоляции")
    building_height = tep['building_height_m']
    shadow_length = calculate_shadow_length(building_height, latitude=55.75)
    
    st.info(f"🏢 Высота здания: **{building_height:.1f} м** | Длина тени (зимнее солнцестояние): **{shadow_length:.1f} м**")
    
    if shadow_length > 50:
        st.warning(f"⚠️ При высоте {building_height:.0f}м тень может достигать {shadow_length:.0f}м. Рекомендуется проверить затенение соседних территорий!")

    # ========================================================================    # КАРТА
    # ========================================================================
    
    st.header("🗺️ Карта территории")
    
    centroid = parcel_geom_wgs84.centroid
    center_lat = centroid.y
    center_lon = centroid.x
    
    m = folium.Map(location=[center_lat, center_lon], zoom_start=16, tiles="OpenStreetMap")

    parcel_geojson = {"type": "Feature", "properties": {}, "geometry": mapping(parcel_geom_wgs84)}
    buildable_geojson = {"type": "Feature", "properties": {}, "geometry": mapping(meters_to_wgs84(buildable_geom_meters))}

    folium.GeoJson(
        parcel_geojson,
        name="Границы участка",
        style_function=lambda x: {"fillColor": "gray", "fillOpacity": 0.1, "color": "red", "weight": 3}
    ).add_to(m)

    folium.GeoJson(
        buildable_geojson,
        name="Пятно застройки",
        style_function=lambda x: {"fillColor": "blue", "fillOpacity": 0.4, "color": "blue", "weight": 2}
    ).add_to(m)

    # Зона тени (визуализация)
    shadow_zone_meters = buildable_geom_meters.buffer(shadow_length)
    shadow_zone_wgs84 = meters_to_wgs84(shadow_zone_meters)
    shadow_geojson = {"type": "Feature", "properties": {}, "geometry": mapping(shadow_zone_wgs84)}
    
    folium.GeoJson(
        shadow_geojson,
        name="Зона потенциальной тени",
        style_function=lambda x: {"fillColor": "orange", "fillOpacity": 0.2, "color": "orange", "weight": 1, "dashArray": "5, 5"}
    ).add_to(m)

    # Ограничения (если есть)
    if restrictions_geom_meters is not None:
        restrictions_wgs84 = meters_to_wgs84(restrictions_geom_meters)
        restrictions_geojson = {"type": "Feature", "properties": {}, "geometry": mapping(restrictions_wgs84)}
        folium.GeoJson(
            restrictions_geojson,
            name="Ограничения (ЗОУИТ)",
            style_function=lambda x: {"fillColor": "red", "fillOpacity": 0.3, "color": "red", "weight": 2}
        ).add_to(m)

    folium.LayerControl().add_to(m)
    st_folium(m, width=1200, height=600)
    # ========================================================================
    # ЭКСПОРТ ДАННЫХ
    # ========================================================================
    
    st.header("📥 Экспорт результатов")
    
    export_cols = st.columns(3)
    
    # Экспорт ТЭП
    with export_cols[0]:
        st.download_button(
            label="📊 Скачать ТЭП (JSON)",
            data=io.BytesIO(json.dumps({**tep, **financials}, ensure_ascii=False, indent=2).encode()),
            file_name="tep_financials.json",
            mime="application/json"
        )
    
    # Экспорт пятна застройки
    with export_cols[1]:
        buildable_export = {"type": "Feature", "properties": {"area_sqm": buildable_geom_meters.area}, "geometry": mapping(meters_to_wgs84(buildable_geom_meters))}
        st.download_button(
            label="🗺️ Скачать пятно застройки (GeoJSON)",
            data=io.BytesIO(json.dumps(buildable_export, ensure_ascii=False, indent=2).encode()),
            file_name="buildable_area.geojson",
            mime="application/json"
        )
    
    # Экспорт 3D модели
    with export_cols[2]:
        obj_content = create_obj_file(buildable_geom_meters, building_height)
        if obj_content:
            st.download_button(
                label="🏢 Скачать 3D модель (OBJ)",
                data=io.BytesIO(obj_content.encode()),
                file_name="building_3d.obj",
                mime="text/plain"
            )
        else:
            st.warning("3D модель недоступна")

    st.success("✅ Полный анализ завершен! Используйте экспортированные файлы для дальнейшей работы.")

else:
    st.warning("⚠️ Загрузите файл с границами участка для начала анализа.")