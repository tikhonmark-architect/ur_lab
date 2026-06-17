import streamlit as st
import folium
from streamlit_folium import st_folium
import geopandas as gpd
from shapely.geometry import Polygon
import math
import io

# 1. Класс анализатора (без изменений)
class UrbanPotentialAnalyzer:
    def __init__(self, parcel_geom_meters, pzz_config):
        self.parcel_geom = parcel_geom_meters
        self.pzz = pzz_config
        self.buildable_geom = None
        self.tep = {}

    def calculate_buildable_area(self):
        min_offset = self.pzz.get("min_offset_from_border", 0)
        buildable = self.parcel_geom.buffer(-min_offset)
        self.buildable_geom = buildable
        return buildable

    def calculate_tep(self):
        if self.buildable_geom is None:
            self.calculate_buildable_area()
            
        s_uch = self.parcel_geom.area
        s_buildable = self.buildable_geom.area
        
        max_density = self.pzz.get("max_building_density", 1.0)
        max_floors = self.pzz.get("max_floors", 1)
        living_ratio = self.pzz.get("living_area_ratio", 0.7)
        norm_housing = self.pzz.get("norm_housing_per_person", 28.0)
        
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

# 2. Настройка страницы
st.set_page_config(page_title="Градостроительный Потенциал", layout="wide")
st.title("🏙️ Анализатор градостроительного потенциала")
st.markdown("Интерактивный расчет ТЭП и выявление строительного пятна на основе ПЗЗ.")

# 3. Загрузка данных
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
    st.info("💡 Поддерживаемые форматы: GeoJSON (.geojson, .json), GeoPackage (.gpkg), Shapefile (.shp - требуется .shx, .dbf, .prj)")
    
    uploaded_file = st.file_uploader(
        "Загрузите файл с границами участка",
        type=['geojson', 'json', 'gpkg', 'zip'],
        help="Файл должен содержать полигональную геометрию"
    )
    
    if uploaded_file is not None:
        try:
            # Чтение загруженного файла
            file_bytes = io.BytesIO(uploaded_file.getvalue())
            gdf_uploaded = gpd.read_file(file_bytes)
            
            # Проверка на наличие геометрии
            if gdf_uploaded.empty:
                st.error("Файл пуст или не содержит геометрии")
            else:
                # Объединение всех полигонов в один (если их несколько)
                parcel_geom_wgs84 = gdf_uploaded.geometry.unary_union
                
                # Конвертация в метры для расчетов
                if gdf_uploaded.crs is None:
                    st.warning("⚠️ В файле не указана система координат. Предполагается WGS84 (EPSG:4326)")
                    gdf_uploaded = gdf_uploaded.set_crs("EPSG:4326")
                
                # Если координаты уже в метрах, пропускаем конвертацию
                if gdf_uploaded.crs.is_projected:
                    gdf_meters = gdf_uploaded
                    gdf_wgs84 = gdf_uploaded.to_crs("EPSG:4326")
                else:
                    gdf_meters = gdf_uploaded.to_crs("EPSG:3857")
                    gdf_wgs84 = gdf_uploaded
                
                parcel_geom_meters = gdf_meters.geometry.unary_union
                
                st.success(f"✅ Файл успешно загружен! Площадь участка: {parcel_geom_meters.area:,.0f} м²")
                
        except Exception as e:
            st.error(f"❌ Ошибка при чтении файла: {str(e)}")
            st.info("Попробуйте другой формат или проверьте целостность файла.")

else:
    # Тестовые данные
    st.info("Используется демонстрационный участок в центре Москвы")
    coords_wgs84 = [
        (37.61500, 55.75200),
        (37.61750, 55.75200),
        (37.61750, 55.75350),
        (37.61500, 55.75350)
    ]
    
    gdf_wgs84 = gpd.GeoDataFrame(geometry=[Polygon(coords_wgs84)], crs="EPSG:4326")
    gdf_meters = gdf_wgs84.to_crs("EPSG:3857")
    parcel_geom_meters = gdf_meters.geometry.iloc[0]
    parcel_geom_wgs84 = gdf_wgs84.geometry.iloc[0]

# 4. Параметры ПЗЗ (только если есть геометрия)
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

    # 5. Запуск анализатора
    analyzer = UrbanPotentialAnalyzer(parcel_geom_meters, pzz_config)
    tep = analyzer.calculate_tep()
    buildable_geom_meters = analyzer.buildable_geom

    # Обратная конвертация для карты
    gdf_buildable_meters = gpd.GeoDataFrame(geometry=[buildable_geom_meters], crs="EPSG:3857")
    gdf_buildable_wgs84 = gdf_buildable_meters.to_crs("EPSG:4326")

    # 6. Отображение ТЭП
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

    # 7. Карта
    st.header("🗺️ Карта территории и строительного пятна")
    
    # Получаем центр участка для позиционирования карты
    centroid = gdf_buildable_wgs84.geometry.iloc[0].centroid
    center_lat = centroid.y
    center_lon = centroid.x
    
    m = folium.Map(location=[center_lat, center_lon], zoom_start=16, tiles="OpenStreetMap")

    # Исходный участок
    gdf_parcel_wgs84 = gpd.GeoDataFrame(geometry=[parcel_geom_wgs84], crs="EPSG:4326")
    folium.GeoJson(
        gdf_parcel_wgs84,
        name="Границы участка",
        style_function=lambda x: {"fillColor": "gray", "fillOpacity": 0.1, "color": "red", "weight": 3}
    ).add_to(m)

    # Пятно застройки
    folium.GeoJson(
        gdf_buildable_wgs84,
        name="Пятно застройки",
        style_function=lambda x: {"fillColor": "blue", "fillOpacity": 0.4, "color": "blue", "weight": 2}
    ).add_to(m)

    folium.LayerControl().add_to(m)
    st_folium(m, width=1200, height=600)

    st.success("✅ Анализ завершен. Двигайте слайдеры слева для изменения параметров!")
    
    # Кнопка скачивания результатов
    st.download_button(
        label="📥 Скачать ТЭП (JSON)",
        data=io.BytesIO(str(tep).encode()),
        file_name="tep_results.json",
        mime="application/json"
    )

else:
    st.warning("⚠️ Загрузите файл с границами участка или выберите тестовые данные для начала анализа.")