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

st.set_page_config(page_title="Градостроительный Потенциал", layout="wide")
st.title("🏙️ Анализатор градостроительного потенциала")
st.markdown("**Упрощённая версия для Streamlit Cloud**")

# ============================================================================
# ФУНКЦИИ (упрощённые без pyproj)
# ============================================================================

def parse_geojson(file_content):
    """Парсинг GeoJSON"""
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

def calculate_area_simple(geom):
    """
    Упрощённый расчёт площади.
    Если координаты в градусах - используем приближённую формулу.
    """
    if geom is None or geom.is_empty:
        return 0
    
    bounds = geom.bounds
    minx, miny, maxx, maxy = bounds
    
    # Определяем, градусы это или метры
    if abs(maxx - minx) < 10 and abs(maxy - miny) < 10:
        # Это градусы - конвертируем в метры приблизительно
        # 1 градус долготы ≈ 111320 * cos(latitude) метров
        # 1 градус широты ≈ 110540 метров
        lat = (miny + maxy) / 2
        lon_to_meters = 111320 * math.cos(math.radians(lat))
        lat_to_meters = 110540
        
        width_meters = (maxx - minx) * lon_to_meters
        height_meters = (maxy - miny) * lat_to_meters
        
        # Приближённая площадь (для сложных полигонов это неточно, но работает)
        return geom.area * lon_to_meters * lat_to_meters
    else:
        # Уже в метрах
        return geom.area

def wgs84_to_web_mercator(geom):
    """Конвертация WGS84 в Web Mercator для отображения"""
    def project(x, y):
        x_merc = x * 20037508.34 / 180
        y_merc = math.log(math.tan((90 + y) * math.pi / 360)) / (math.pi / 180)
        y_merc = y_merc * 20037508.34 / 180
        return x_merc, y_merc
    
    from shapely.ops import transform
    return transform(project, geom)

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
        
        if manual_area:
            self.area = manual_area
        else:
            self.area = calculate_area_simple(parcel_geom)

    def calculate_buildable(self):
        offset = self.pzz.get("offset", 5)
        density = self.pzz.get("density", 0.4)
        
        buildable = self.parcel_geom.buffer(-offset)
        max_buildable = self.area * density
        
        self.buildable_geom = buildable
        self.buildable_area = min(buildable.area, max_buildable) if buildable else 0
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
            "parking": math.ceil(s_total / 100 * 1.2)
        }
        return self.tep

    def calculate_financials(self):
        if not self.tep:
            self.calculate_tep()
        
        cost = self.pzz.get("cost", 90000)
        price = self.pzz.get("price", 250000)
        land = self.pzz.get("land", 50000000)
        
        s_living = self.tep["s_living"]
        s_commercial = self.tep["s_commercial"]
        s_total = self.tep["s_total"]
        
        revenue = s_living * price + s_commercial * price * 0.85
        construction = s_total * cost
        total_cost = construction + land
        
        profit = revenue - total_cost
        roi = (profit / total_cost * 100) if total_cost > 0 else 0
        
        self.financials = {
            "revenue": revenue,
            "cost": total_cost,
            "profit": profit,
            "roi": roi
        }
        return self.financials

# ============================================================================
# ИНТЕРФЕЙС
# ============================================================================

st.header("📂 Загрузка участка")

data_source = st.radio("Источник:", ["Тестовый участок", "Загрузить GeoJSON"], horizontal=True)

parcel_geom = None
manual_area = None

if data_source == "Загрузить GeoJSON":
    uploaded = st.file_uploader("Загрузите .geojson или .json", type=['geojson', 'json'])
    
    if uploaded:
        try:
            content = uploaded.getvalue()
            parcel_geom = parse_geojson(content)
            
            # Показываем диагностику
            area_calc = calculate_area_simple(parcel_geom)
            st.info(f"📏 **Автоматически определена площадь:** {area_calc:,.0f} м²")
            
            use_manual = st.checkbox("✏️ Ввести площадь вручную", value=False)
            if use_manual:
                manual_area = st.number_input("Площадь (м²):", min_value=1.0, value=1400.0, step=10.0)
                st.success(f"✅ Используется площадь: {manual_area:,.0f} м²")
            
            st.success("✅ Файл загружен!")
            
        except Exception as e:
            st.error(f"❌ Ошибка: {str(e)}")
else:
    # Тестовый участок (координаты в градусах)
    coords = [(37.615, 55.752), (37.6175, 55.752), (37.6175, 55.7535), (37.615, 55.7535)]
    parcel_geom = Polygon(coords)
    st.info("Используется тестовый участок")

# ============================================================================
# ПАРАМЕТРЫ
# ============================================================================

if parcel_geom:
    st.sidebar.header("⚙️ Градостроительные параметры")
    offset = st.sidebar.number_input("Отступ (м)", 0.0, 50.0, 5.0, 0.5)
    density = st.sidebar.number_input("Плотность", 0.05, 1.0, 0.4, 0.01)
    floors = st.sidebar.number_input("Этажность", 1, 50, 9, 1)
    floor_height = st.sidebar.number_input("Высота этажа (м)", 2.5, 5.0, 3.2, 0.1)
    living_ratio = st.sidebar.number_input("Доля жилья", 0.0, 1.0, 0.75, 0.01)
    norm = st.sidebar.number_input("Норма (м²/чел)", 10, 50, 28, 1)
    
    st.sidebar.header("💰 Финансы")
    cost = st.sidebar.number_input("Себестоимость (₽/м²)", 10000, 300000, 90000, 1000)
    price = st.sidebar.number_input("Цена продажи (₽/м²)", 50000, 1000000, 250000, 5000)
    land = st.sidebar.number_input("Земля (млн ₽)", 0.0, 1000.0, 50.0, 1.0) * 1000000
    
    pzz = {
        "offset": offset,
        "density": density,
        "floors": floors,
        "floor_height": floor_height,
        "living_ratio": living_ratio,
        "norm": norm,
        "cost": cost,
        "price": price,
        "land": land
    }
    
    analyzer = UrbanAnalyzer(parcel_geom, pzz, manual_area)
    tep = analyzer.calculate_tep()
    fin = analyzer.calculate_financials()
    
    # ========================================================================
    # РЕЗУЛЬТАТЫ
    # ========================================================================
    
    st.header("📊 ТЭП")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Участок", f"{tep['s_uch']:,.0f} м²", f"{tep['s_uch_ha']} га")
    c2.metric("Пятно", f"{tep['s_buildable']:,.0f} м²")
    c3.metric("GBA", f"{tep['s_total']:,.0f} м²")
    c4.metric("Этажи", f"{tep['floors']} эт")
    
    st.subheader("🏗️ Структура")
    ac1, ac2, ac3 = st.columns(3)
    if tep['s_total'] > 0:
        lp = tep['s_living']/tep['s_total']*100
        cp = tep['s_commercial']/tep['s_total']*100
    else:
        lp = cp = 0
    ac1.info(f"🏠 Жилая: {tep['s_living']:,.0f} м² ({lp:.1f}%)")
    ac2.info(f"🏢 Коммерция: {tep['s_commercial']:,.0f} м² ({cp:.1f}%)")
    ac3.info(f"👪 Население: {tep['population']} чел")
    
    st.header("💰 Финансы")
    f1, f2, f3, f4 = st.columns(4)
    f1.metric("Выручка", f"{fin['revenue']/1e6:,.1f} млн ₽")
    f2.metric("Затраты", f"{fin['cost']/1e6:,.1f} млн ₽")
    profit_color = "normal" if fin['profit'] >= 0 else "inverse"
    f3.metric("Прибыль", f"{fin['profit']/1e6:,.1f} млн ₽", delta_color=profit_color)
    f4.metric("ROI", f"{fin['roi']:.1f}%", delta_color=profit_color)
    
    # ========================================================================
    # КАРТА
    # ========================================================================
    
    st.header("🗺️ Карта")
    
    bounds = parcel_geom.bounds
    center_lat = (bounds[1] + bounds[3]) / 2
    center_lon = (bounds[0] + bounds[2]) / 2
    
    m = folium.Map(location=[center_lat, center_lon], zoom_start=16)
    
    # Участок
    if abs(bounds[0]) < 180:  # Если в градусах
        parcel_geo = {"type": "Feature", "geometry": mapping(parcel_geom)}
    else:  # Если в метрах - конвертируем
        parcel_merc = wgs84_to_web_mercator(parcel_geom)
        parcel_geo = {"type": "Feature", "geometry": mapping(parcel_merc)}
    
    folium.GeoJson(parcel_geo, style_function=lambda x: {
        "fillColor": "gray", "fillOpacity": 0.1, "color": "red", "weight": 3
    }).add_to(m)
    
    # Пятно застройки
    if analyzer.buildable_geom and not analyzer.buildable_geom.is_empty:
        if abs(bounds[0]) < 180:
            build_geo = {"type": "Feature", "geometry": mapping(analyzer.buildable_geom)}
        else:
            build_merc = wgs84_to_web_mercator(analyzer.buildable_geom)
            build_geo = {"type": "Feature", "geometry": mapping(build_merc)}
        
        folium.GeoJson(build_geo, style_function=lambda x: {
            "fillColor": "blue", "fillOpacity": 0.4, "color": "blue", "weight": 2
        }).add_to(m)
    
    st_folium(m, width=1200, height=500)
    
    # ========================================================================
    # ЭКСПОРТ
    # ========================================================================
    
    st.header("📥 Экспорт")
    ec1, ec2 = st.columns(2)
    
    with ec1:
        report = {"tep": tep, "financials": fin}
        st.download_button("📊 JSON", 
            data=io.BytesIO(json.dumps(report, indent=2).encode()),
            file_name="report.json", mime="application/json")
    
    with ec2:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Показатель", "Значение"])
        for k, v in tep.items():
            w.writerow([k, v])
        for k, v in fin.items():
            w.writerow([k, v])
        st.download_button("📄 CSV",
            data=io.BytesIO(buf.getvalue().encode('utf-8-sig')),
            file_name="tep.csv", mime="text/csv")
    
    st.success("✅ Готово!")

else:
    st.warning("⚠️ Загрузите файл или выберите тестовый участок")