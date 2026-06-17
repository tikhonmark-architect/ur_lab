import streamlit as st
import folium
from streamlit_folium import st_folium
from shapely.geometry import Polygon, shape, mapping
from shapely.ops import transform
import pyproj
import math
import json
import io

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
# КЛАСС АНАЛИЗАТОРА
# ============================================================================

class UrbanPotentialAnalyzer:
    def __init__(self, parcel_geom_meters, pzz_config):
        """
        Инициализация анализатора.
        :param parcel_geom_meters: геометрия участка в метрах (EPSG:3857)
        :param pzz_config: словарь с параметрами градостроительных регламентов
        """
        self.parcel_geom = parcel_geom_meters
        self.pzz = pzz_config
        self.buildable_geom = None
        self.tep = {}

    def calculate_buildable_area(self):
        """Вычисление строительного пятна с учетом отступов"""
        min_offset = self.pzz.get("min_offset_from_border", 0)
        buildable = self.parcel_geom.buffer(-min_offset)
        self.buildable_geom = buildable
        return buildable

    def calculate_tep(self):
        """Расчет технико-экономических показателей"""
        if self.buildable_geom is None:
            self.calculate_buildable_area()
            
        s_uch = self.parcel_geom.area
        s_buildable = self.buildable_geom.area
        
        max_density = self.pzz.get("max_building_density", 1.0)
        max_floors = self.pzz.get("max_floors", 1)
        living_ratio = self.pzz.get("living_area_ratio", 0.7)
        norm_housing = self.pzz.get("norm_housing_per_person", 28.0)
        
        # Ограничение по плотности ПЗЗ
        s_zas_max = s_uch * max_density
        s_zas = min(s_buildable, s_zas_max)
        
        s_total = s_zas * max_floors
        s_living = s_total * living_ratio
        population = math.floor(s_living / norm_housing) if norm_housing > 0 else 0
        
        self.tep = {
            "s_uch": round(s_uch, 1),
            "s_buildable": round(s_buildable, 1),
            "s_zas": round(s_zas, 1),
            "s_total": round(s_total, 1),
            "s_living": round(s_living, 1),
            "floors": max_floors,
            "population": population,
            "schools": math.ceil((population / 1000) * 120),
            "kindergartens": math.ceil((population / 1000) * 60),
            "parking": math.ceil((s_total / 100) * 1.2)
        }
        return self.tep

# ============================================================================
# STREAMLIT ИНТЕРФЕЙС
# ============================================================================

# Настройка страницы
st.set_page_config(page_title="Градостроительный Потенциал", layout="wide")
st.title("🏙️ Анализатор градостроительного потенциала")
st.markdown("Интерактивный расчет ТЭП и выявление строительного пятна на основе ПЗЗ.")

# ============================================================================
# ЗАГРУЗКА ДАННЫХ
# ============================================================================

st.header("📂 Загрузка территории")

# Вариант выбора: тестовые данные или загрузка файла
data_source = st.radio(
    "Выберите источник данных:",
    ["Тестовый участок (пример)", "Загрузить свой файл"],
    horizontal=True
)

parcel_geom_meters = None
parcel_geom_wgs84 = None

if data_source == "Загрузить свой файл":
    st.info("💡 Поддерживаемый формат: **GeoJSON** (.geojson, .json)")
    
    uploaded_file = st.file_uploader(
        "Загрузите GeoJSON файл с границами участка",
        type=['geojson', 'json'],
        help="Файл должен содержать полигональную геометрию в системе координат WGS84 (EPSG:4326)"
    )
    
    if uploaded_file is not None:
        try:
            # Читаем как обычный JSON
            geojson_data = json.loads(uploaded_file.getvalue().decode("utf-8"))
            
            # Извлекаем геометрию
            if geojson_data['type'] == 'FeatureCollection':
                geom_dict = geojson_data['features'][0]['geometry']
            elif geojson_data['type'] == 'Feature':
                geom_dict = geojson_data['geometry']
            else:
                geom_dict = geojson_data
            
            parcel_geom_wgs84 = shape(geom_dict)
            parcel_geom_meters = wgs84_to_meters(parcel_geom_wgs84)
            
            st.success(f"✅ Файл успешно загружен! Площадь участка: {parcel_geom_meters.area:,.0f} м²")
            
        except Exception as e:
            st.error(f"❌ Ошибка при чтении файла: {str(e)}")
            st.info("Убедитесь, что файл в формате GeoJSON и содержит валидную геометрию.")

else:
    # Тестовые данные (условный участок в центре Москвы)
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
# ПАРАМЕТРЫ ПЗЗ И РАСЧЕТ
# ============================================================================

if parcel_geom_meters is not None:
    st.sidebar.header("⚙️ Параметры ПЗЗ и Регламента")
    offset = st.sidebar.slider("Мин. отступ от границ (м)", 0, 30, 5, help="Отступ от красных линий")
    density = st.sidebar.slider("Макс. плотность застройки", 0.1, 1.0, 0.4, 0.05, help="Процент застройки участка")
    floors = st.sidebar.slider("Предельная этажность", 1, 50, 9)
    living_ratio = st.sidebar.slider("Доля жилой площади", 0.1, 1.0, 0.75, 0.05)
    norm_housing = st.sidebar.slider("Норма жилья на чел. (кв.м)", 15, 50, 25)

    pzz_config = {
        "min_offset_from_border": offset,
        "max_building_density": density,
        "max_floors": floors,
        "living_area_ratio": living_ratio,
        "norm_housing_per_person": norm_housing
    }

    # Запуск анализатора
    analyzer = UrbanPotentialAnalyzer(parcel_geom_meters, pzz_config)
    tep = analyzer.calculate_tep()
    buildable_geom_meters = analyzer.buildable_geom

    # ========================================================================
    # ОТОБРАЖЕНИЕ ТЭП
    # ========================================================================
    
    st.header("📊 Технико-экономические показатели")
    cols = st.columns(4)
    cols[0].metric("Общая площадь (GBA)", f"{tep['s_total']:,.0f} м²")
    cols[1].metric("Жилая площадь", f"{tep['s_living']:,.0f} м²")
    cols[2].metric("Проектное население", f"{tep['population']:,.0f} чел")
    cols[3].metric("Этажность", f"{tep['floors']} эт")

    st.subheader("Социальная инфраструктура (Потребность)")
    infra_cols = st.columns(3)
    infra_cols[0].info(f"🏫 Школы: **{tep['schools']}** мест")
    infra_cols[1].info(f"🧸 Детские сады: **{tep['kindergartens']}** мест")
    infra_cols[2].info(f"🚗 Парковка: **{tep['parking']}** м/м")

    # ========================================================================
    # КАРТА
    # ========================================================================
    
    st.header("🗺️ Карта территории и строительного пятна")
    
    # Получаем центр участка для позиционирования карты
    centroid = parcel_geom_wgs84.centroid
    center_lat = centroid.y
    center_lon = centroid.x
    
    m = folium.Map(location=[center_lat, center_lon], zoom_start=16, tiles="OpenStreetMap")

    # Создаём GeoJSON словари для folium (без geopandas!)
    parcel_geojson = {"type": "Feature", "properties": {}, "geometry": mapping(parcel_geom_wgs84)}
    buildable_geojson = {"type": "Feature", "properties": {}, "geometry": mapping(meters_to_wgs84(buildable_geom_meters))}

    # Исходный участок (Красный контур)
    folium.GeoJson(
        parcel_geojson,
        name="Границы участка",
        style_function=lambda x: {"fillColor": "gray", "fillOpacity": 0.1, "color": "red", "weight": 3}
    ).add_to(m)

    # Пятно застройки (Синий полигон)
    folium.GeoJson(
        buildable_geojson,
        name="Пятно застройки",
        style_function=lambda x: {"fillColor": "blue", "fillOpacity": 0.4, "color": "blue", "weight": 2}
    ).add_to(m)

    folium.LayerControl().add_to(m)
    st_folium(m, width=1200, height=600)

    st.success("✅ Анализ завершен. Двигайте слайдеры слева для изменения параметров!")
    
    # Кнопка скачивания результатов
    st.download_button(
        label="📥 Скачать ТЭП (JSON)",
        data=io.BytesIO(json.dumps(tep, ensure_ascii=False, indent=2).encode()),
        file_name="tep_results.json",
        mime="application/json"
    )

else:
    st.warning("⚠️ Загрузите файл с границами участка или выберите тестовые данные для начала анализа.")
