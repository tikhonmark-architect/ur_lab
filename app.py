import streamlit as st
import folium
from streamlit_folium import st_folium
from shapely.geometry import Polygon, shape, mapping
from shapely.ops import unary_union
import json
import io
import csv
import math
from datetime import datetime

# ============================================================================
# НАСТРОЙКА СТРАНИЦЫ
# ============================================================================

st.set_page_config(page_title="Градостроительный Потенциал PRO", layout="wide")
st.title("️ Анализатор градостроительного потенциала PRO")
st.markdown("**Полный комплексный анализ:** ТЭП + Финансы + 3D + Инсоляция")

# ============================================================================
# ФУНКЦИИ
# ============================================================================

def parse_geojson(file_content):
    """Парсинг GeoJSON файла"""
    try:
        data = json.loads(file_content.decode("utf-8"))
        if data['type'] == 'FeatureCollection':
            geoms = [shape(f['geometry']) for f in data['features']]
            return unary_union(geoms)
        elif data['type'] == 'Feature':
            return shape(data['geometry'])
        else:
            return shape(data)
    except Exception as e:
        raise ValueError(f"Ошибка GeoJSON: {str(e)}")

def calculate_area_approximate(geom):
    """
    Приближённый расчёт площади для полигона в градусах.
    Конвертирует градусы в метры и считает площадь.
    """
    if geom is None or geom.is_empty:
        return 0
    
    bounds = geom.bounds
    minx, miny, maxx, maxy = bounds
    
    # Проверяем, градусы это или метры
    if abs(maxx - minx) < 10 and abs(maxy - miny) < 10:
        # Это градусы - конвертируем в метры
        lat_center = (miny + maxy) / 2
        lon_to_m = 111320 * math.cos(math.radians(lat_center))
        lat_to_m = 110540
        
        # Считаем площадь через интеграл (упрощённо)
        area_degrees = geom.area
        area_meters = area_degrees * lon_to_m * lat_to_m
        return area_meters
    else:
        # Уже в метрах
        return geom.area

def is_in_degrees(geom):
    """Проверяет, в градусах ли координаты"""
    if geom is None:
        return False
    bounds = geom.bounds
    minx, miny, maxx, maxy = bounds
    return abs(maxx - minx) < 10 and abs(maxy - miny) < 10

def create_obj_file(geometry, height_meters):
    """Создание OBJ файла из 2D полигона"""
    if geometry is None or geometry.is_empty:
        return ""
    
    coords = list(geometry.exterior.coords) if geometry.geom_type == 'Polygon' else None
    if not coords or len(coords) < 3:
        return ""
    
    min_x = min(c[0] for c in coords)
    min_y = min(c[1] for c in coords)
    
    lines = []
    lines.append("# Building 3D Model")
    lines.append(f"# Height: {height_meters:.1f}m")
    lines.append("")
    
    # Вершины основания
    for x, y in coords[:-1]:
        lines.append(f"v {x - min_x:.2f} {y - min_y:.2f} 0.0")
    
    # Вершины крыши
    for x, y in coords[:-1]:
        lines.append(f"v {x - min_x:.2f} {y - min_y:.2f} {height_meters:.2f}")
    
    lines.append("")
    
    n = len(coords) - 1
    lines.append("f " + " ".join(str(i+1) for i in range(n)))
    lines.append("f " + " ".join(str(i+1+n) for i in range(n)))
    
    for i in range(n):
        next_i = (i + 1) % n
        lines.append(f"f {i+1} {next_i+1} {next_i+1+n} {i+1+n}")
    
    return "\n".join(lines)

def calculate_shadow_length(building_height, latitude=55.75):
    """Расчет длины тени"""
    solar_angle = 90 - abs(latitude + 23.45)
    solar_angle_rad = math.radians(max(solar_angle, 5))
    return building_height / math.tan(solar_angle_rad)

# ============================================================================
# КЛАСС АНАЛИЗАТОРА
# ============================================================================

class UrbanAnalyzer:
    def __init__(self, parcel_geom, pzz_config, manual_area=None):
        self.parcel_geom = parcel_geom
        self.pzz = pzz_config
        self.buildable_geom = None
        self.tep = {}
        self.financials = {}
        
        # Используем ручную площадь или считаем автоматически
        if manual_area and manual_area > 0:
            self.area = manual_area
        else:
            self.area = calculate_area_approximate(parcel_geom)

    def calculate_buildable(self):
        offset = self.pzz.get("offset", 5)
        density = self.pzz.get("density", 0.4)
        
        buildable = self.parcel_geom.buffer(-offset)
        max_buildable = self.area * density
        
        self.buildable_geom = buildable
        
        # Для пятна застройки считаем площадь пропорционально
        if buildable and not buildable.is_empty:
            buildable_area_approx = calculate_area_approximate(buildable)
            self.buildable_area = min(buildable_area_approx, max_buildable)
        else:
            self.buildable_area = 0
        
        return buildable

    def calculate_tep(self):
        if not self.buildable_geom:
            self.calculate_buildable()
        
        floors = self.pzz.get("floors", 9)
        floor_height = self.pzz.get("floor_height", 3.2)
        living_ratio = self.pzz.get("living_ratio", 0.75)
        norm = self.pzz.get("norm", 28)
        
        s_total = self.buildable_area * floors
        s_living = s_total * living_ratio
        s_commercial = s_total - s_living
        population = int(s_living / norm) if norm > 0 else 0
        
        self.tep = {
            "s_uch": round(self.area, 1),
            "s_uch_ha": round(self.area / 10000, 2),
            "s_buildable": round(self.buildable_area, 1),
            "s_total": round(s_total, 1),
            "s_living": round(s_living, 1),
            "s_commercial": round(s_commercial, 1),
            "floors": floors,
            "building_height": floors * floor_height,
            "population": population,
            "schools": math.ceil(population / 1000 * 120),
            "kindergartens": math.ceil(population / 1000 * 60),
            "parking": math.ceil(s_total / 100 * 1.2),
            "green_space": math.ceil(population * 6)
        }
        return self.tep

    def calculate_financials(self):
        if not self.tep:
            self.calculate_tep()
        
        cost = self.pzz.get("cost", 90000)
        price = self.pzz.get("price", 250000)
        commercial_ratio = self.pzz.get("commercial_ratio", 0.85)
        land = self.pzz.get("land", 50000000)
        
        s_living = self.tep["s_living"]
        s_commercial = self.tep["s_commercial"]
        s_total = self.tep["s_total"]
        
        revenue_living = s_living * price
        revenue_commercial = s_commercial * price * commercial_ratio
        total_revenue = revenue_living + revenue_commercial
        
        construction_cost = s_total * cost
        infra_cost = (self.tep["schools"] * 1500000 + 
                      self.tep["kindergartens"] * 1200000 + 
                      self.tep["parking"] * 800000)
        total_cost = construction_cost + land + infra_cost
        
        profit = total_revenue - total_cost
        roi = (profit / total_cost * 100) if total_cost > 0 else 0
        profit_per_sqm = (profit / s_total) if s_total > 0 else 0
        
        self.financials = {
            "revenue_living": revenue_living,
            "revenue_commercial": revenue_commercial,
            "total_revenue": total_revenue,
            "construction_cost": construction_cost,
            "infra_cost": infra_cost,
            "land_cost": land,
            "total_cost": total_cost,
            "profit": profit,
            "roi": roi,
            "profit_per_sqm": profit_per_sqm
        }
        return self.financials

# ============================================================================
# ЗАГРУЗКА ДАННЫХ
# ============================================================================

st.header("📂 Загрузка территории")

data_source = st.radio(
    "Выберите источник данных:",
    ["Тестовый участок (пример)", "Загрузить свой файл"],
    horizontal=True
)

parcel_geom = None
manual_area = None

if data_source == "Загрузить свой файл":
    st.info("💡 **Рекомендуется GeoJSON** в системе координат WGS84 (EPSG:4326)")
    
    uploaded_file = st.file_uploader(
        "Загрузите GeoJSON файл с границами участка",
        type=['geojson', 'json'],
        key="main_parcel"
    )
    
    if uploaded_file is not None:
        try:
            content = uploaded_file.getvalue()
            parcel_geom = parse_geojson(content)
            
            # Диагностика
            area_calc = calculate_area_approximate(parcel_geom)
            bounds = parcel_geom.bounds
            
            with st.expander("🔍 Диагностика геометрии"):
                st.write(f"**Диапазон координат:**")
                st.write(f"- X (долгота): {bounds[0]:.6f} - {bounds[2]:.6f}")
                st.write(f"- Y (широта): {bounds[1]:.6f} - {bounds[3]:.6f}")
                st.write(f"**Автоматически рассчитанная площадь:** {area_calc:,.0f} м²")
                
                if is_in_degrees(parcel_geom):
                    st.success("✅ Координаты в градусах (WGS84)")
                else:
                    st.warning("⚠️ Координаты в метрах (не WGS84)")
            
            # Ручная коррекция
            use_manual = st.checkbox("✏️ Использовать ручную коррекцию площади", value=False)
            if use_manual:
                manual_area = st.number_input(
                    "Введите реальную площадь участка (м²):",
                    min_value=1.0,
                    value=1400.0,
                    step=10.0
                )
                st.success(f"✅ Будет использоваться площадь: **{manual_area:,.0f} м²**")
            
            st.success(f"✅ Файл загружен! Площадь: {area_calc:,.0f} м²")
            
        except Exception as e:
            st.error(f"❌ Ошибка: {str(e)}")

else:
    # Тестовый участок (координаты в градусах)
    coords = [(37.615, 55.752), (37.6175, 55.752), (37.6175, 55.7535), (37.615, 55.7535)]
    parcel_geom = Polygon(coords)
    st.info("Используется демонстрационный участок в центре Москвы")

# ============================================================================
# ПАРАМЕТРЫ (РУЧНОЙ ВВОД)
# ============================================================================

if parcel_geom is not None:
    st.sidebar.header("⚙️ Градостроительные параметры")
    
    offset = st.sidebar.number_input(
        "Мин. отступ от границ (м)",
        min_value=0.0,
        max_value=100.0,
        value=5.0,
        step=0.5
    )
    
    density = st.sidebar.number_input(
        "Макс. плотность застройки",
        min_value=0.05,
        max_value=1.0,
        value=0.40,
        step=0.01
    )
    
    floors = st.sidebar.number_input(
        "Предельная этажность",
        min_value=1,
        max_value=100,
        value=9,
        step=1
    )
    
    floor_height = st.sidebar.number_input(
        "Высота этажа (м)",
        min_value=2.5,
        max_value=6.0,
        value=3.2,
        step=0.1
    )
    
    living_ratio = st.sidebar.number_input(
        "Доля жилой площади",
        min_value=0.0,
        max_value=1.0,
        value=0.75,
        step=0.01
    )
    
    norm = st.sidebar.number_input(
        "Норма жилья на чел. (кв.м)",
        min_value=10.0,
        max_value=100.0,
        value=28.0,
        step=1.0
    )
    
    st.sidebar.header("💰 Финансовые параметры")
    
    cost = st.sidebar.number_input(
        "Себестоимость строительства (₽/м²)",
        min_value=10000,
        max_value=500000,
        value=90000,
        step=1000
    )
    
    price = st.sidebar.number_input(
        "Цена продажи жилья (₽/м²)",
        min_value=50000,
        max_value=2000000,
        value=250000,
        step=5000
    )
    
    commercial_ratio = st.sidebar.number_input(
        "Коэфф. цены коммерции",
        min_value=0.1,
        max_value=3.0,
        value=0.85,
        step=0.05
    )
    
    land_cost_mln = st.sidebar.number_input(
        "Стоимость земли (млн ₽)",
        min_value=0.0,
        max_value=10000.0,
        value=50.0,
        step=1.0
    )
    
    pzz_config = {
        "offset": offset,
        "density": density,
        "floors": floors,
        "floor_height": floor_height,
        "living_ratio": living_ratio,
        "norm": norm,
        "cost": cost,
        "price": price,
        "commercial_ratio": commercial_ratio,
        "land": land_cost_mln * 1000000
    }
    
    # Запуск анализатора
    analyzer = UrbanAnalyzer(parcel_geom, pzz_config, manual_area)
    tep = analyzer.calculate_tep()
    financials = analyzer.calculate_financials()
    
    # Проверка на нулевые значения
    if tep['s_total'] <= 0:
        st.error("❌ **Ошибка:** Общая площадь равна нулю. Проверьте параметры (отступы, плотность, этажность).")
        st.stop()
    
    # ========================================================================
    # ОТОБРАЖЕНИЕ ТЭП
    # ========================================================================
    
    st.header("📊 Технико-экономические показатели")
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Площадь участка", f"{tep['s_uch']:,.0f} м²", f"{tep['s_uch_ha']} га")
    col2.metric("Пятно застройки", f"{tep['s_buildable']:,.0f} м²")
    col3.metric("Общая площадь (GBA)", f"{tep['s_total']:,.0f} м²")
    col4.metric("Этажность", f"{tep['floors']} эт", f"{tep['building_height']} м")
    
    st.subheader("🏗️ Структура площадей")
    
    if tep['s_total'] > 0:
        living_percent = (tep['s_living'] / tep['s_total']) * 100
        commercial_percent = (tep['s_commercial'] / tep['s_total']) * 100
    else:
        living_percent = 0
        commercial_percent = 0
    
    area_cols = st.columns(3)
    area_cols[0].info(f"🏠 **Жилая:** {tep['s_living']:,.0f} м² ({living_percent:.1f}%)")
    area_cols[1].info(f"🏢 **Коммерция:** {tep['s_commercial']:,.0f} м² ({commercial_percent:.1f}%)")
    area_cols[2].info(f"🌳 **Озеленение:** {tep['green_space']:,.0f} м²")
    
    st.subheader("👥 Социальная инфраструктура")
    infra_cols = st.columns(4)
    infra_cols[0].info(f" **Население:** {tep['population']:,} чел")
    infra_cols[1].info(f"🏫 **Школы:** {tep['schools']} мест")
    infra_cols[2].info(f"🧸 **Дет. сады:** {tep['kindergartens']} мест")
    infra_cols[3].info(f" **Парковка:** {tep['parking']} м/м")
    
    # ========================================================================
    # ФИНАНСОВЫЕ ПОКАЗАТЕЛИ
    # ========================================================================
    
    st.header(" Финансовая модель")
    
    fin_cols = st.columns(4)
    fin_cols[0].metric("Выручка (GDV)", f"{financials['total_revenue']/1000000:,.1f} млн ₽")
    fin_cols[1].metric("Затраты", f"{financials['total_cost']/1000000:,.1f} млн ₽")
    
    profit_color = "normal" if financials['profit'] >= 0 else "inverse"
    fin_cols[2].metric("Валовая прибыль", f"{financials['profit']/1000000:,.1f} млн ₽", delta_color=profit_color)
    fin_cols[3].metric("Рентабельность (ROI)", f"{financials['roi']}%", delta_color=profit_color)
    
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
    building_height = tep['building_height']
    
    # Определяем широту из геометрии
    bounds = parcel_geom.bounds
    latitude = (bounds[1] + bounds[3]) / 2
    
    shadow_length = calculate_shadow_length(building_height, latitude)
    
    st.info(f" **Высота здания:** {building_height:.1f} м | **Широта:** {latitude:.2f}° | **Длина тени:** {shadow_length:.1f} м")
    
    if shadow_length > 50:
        st.warning(f"⚠️ Тень достигает **{shadow_length:.0f}м**. Рекомендуется детальная инсоляционная экспертиза!")
    
    if building_height > 75:
        st.error(f"🚨 Здание выше 75м требует обязательной разработки СТУ")
    
    # ========================================================================
    # КАРТА
    # ========================================================================
    
    st.header("🗺️ Карта территории и строительного пятна")
    
    bounds = parcel_geom.bounds
    center_lat = (bounds[1] + bounds[3]) / 2
    center_lon = (bounds[0] + bounds[2]) / 2
    
    # Проверяем тип координат
    if is_in_degrees(parcel_geom):
        # Координаты в градусах - отображаем напрямую
        m = folium.Map(location=[center_lat, center_lon], zoom_start=16, tiles="OpenStreetMap")
        
        # Участок
        parcel_geo = {
            "type": "Feature",
            "properties": {"name": "Участок", "area_m2": tep['s_uch']},
            "geometry": mapping(parcel_geom)
        }
        
        folium.GeoJson(
            parcel_geo,
            name="Границы участка",
            style_function=lambda x: {
                "fillColor": "gray",
                "fillOpacity": 0.1,
                "color": "red",
                "weight": 3
            },
            tooltip=folium.GeoJsonTooltip(fields=["name", "area_m2"], aliases=["Объект:", "Площадь:"])
        ).add_to(m)
        
        # Пятно застройки
        if analyzer.buildable_geom and not analyzer.buildable_geom.is_empty:
            build_geo = {
                "type": "Feature",
                "properties": {"name": "Пятно застройки", "area_m2": tep['s_buildable']},
                "geometry": mapping(analyzer.buildable_geom)
            }
            
            folium.GeoJson(
                build_geo,
                name="Пятно застройки",
                style_function=lambda x: {
                    "fillColor": "blue",
                    "fillOpacity": 0.4,
                    "color": "blue",
                    "weight": 2
                },
                tooltip=folium.GeoJsonTooltip(fields=["name", "area_m2"], aliases=["Объект:", "Площадь:"])
            ).add_to(m)
        
        folium.LayerControl().add_to(m)
        st_folium(m, width=1200, height=600)
        
    else:
        # Координаты в метрах - показываем предупреждение
        st.warning(f"""
        ⚠️ **Координаты в метрах** (не градусы WGS84)
        
        Диапазон координат:
        - X: {bounds[0]:,.0f} - {bounds[2]:,.0f}
        - Y: {bounds[1]:,.0f} - {bounds[3]:,.0f}
        
        Для отображения на карте конвертируйте файл в **WGS84 (EPSG:4326)** через QGIS:
        1. Откройте слой в QGIS
        2. Правой кнопкой → Export → Save Features As
        3. Format: GeoJSON
        4. CRS: **EPSG:4326 - WGS 84** (укажите вручную!)
        5. Сохраните и загрузите заново
        """)
        
        # Показываем простую карту
        m = folium.Map(location=[55.75, 37.62], zoom_start=10)
        folium.Marker(
            [55.75, 37.62],
            popup="Участок загружен, но координаты в метрах"
        ).add_to(m)
        st_folium(m, width=1200, height=300)
    
    # ========================================================================
    # ЭКСПОРТ ДАННЫХ
    # ========================================================================
    
    st.header("📥 Экспорт результатов")
    
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
        if analyzer.buildable_geom and not analyzer.buildable_geom.is_empty:
            buildable_export = {
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {
                        "name": "Пятно застройки",
                        "area_m2": tep['s_buildable'],
                        "area_ha": tep['s_buildable'] / 10000
                    },
                    "geometry": mapping(analyzer.buildable_geom)
                }]
            }
            st.download_button(
                label="🗺️ Пятно застройки (GeoJSON)",
                data=io.BytesIO(json.dumps(buildable_export, ensure_ascii=False, indent=2).encode()),
                file_name="buildable_area.geojson",
                mime="application/json"
            )
    
    with export_cols[2]:
        obj_content = create_obj_file(analyzer.buildable_geom, building_height)
        if obj_content:
            st.download_button(
                label="🏢 3D модель (OBJ)",
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
        writer.writerow(["Высота здания", tep['building_height'], "м"])
        writer.writerow(["Население", tep['population'], "чел"])
        writer.writerow(["Школы", tep['schools'], "мест"])
        writer.writerow(["Детские сады", tep['kindergartens'], "мест"])
        writer.writerow(["Парковка", tep['parking'], "м/м"])
        writer.writerow(["Выручка", financials['total_revenue'], "₽"])
        writer.writerow(["Затраты", financials['total_cost'], "₽"])
        writer.writerow(["Прибыль", financials['profit'], "₽"])
        writer.writerow(["ROI", financials['roi'], "%"])
        
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
st.caption("🛠️ Urban Potential Analyzer PRO | Упрощённая версия для Streamlit Cloud")