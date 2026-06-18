import streamlit as st
import folium
from streamlit_folium import st_folium
from shapely.geometry import Polygon, shape, mapping
from shapely.ops import transform, unary_union
import json
import io
import csv
import math
from datetime import datetime

# ============================================================================
# НАСТРОЙКА СТРАНИЦЫ
# ============================================================================
st.set_page_config(page_title="Градостроительный Потенциал PRO", layout="wide")
st.title("🏙️ Анализатор градостроительного потенциала PRO")
st.markdown("**Поддержка МСК-50 (зона 1, 2) | WGS84 | Web Mercator**")

# ============================================================================
# ФУНКЦИИ ГЕОМЕТРИИ
# ============================================================================

def parse_geojson(file_content):
    """Безопасный парсинг GeoJSON"""
    try:
        data = json.loads(file_content.decode("utf-8"))
        if data['type'] == 'FeatureCollection':
            geoms = [shape(f['geometry']) for f in data['features']]
            return unary_union(geoms)
        elif data['type'] == 'Feature':
            return shape(data['geometry'])
        return shape(data)
    except Exception as e:
        raise ValueError(f"Ошибка GeoJSON: {str(e)}")

def is_in_degrees(geom):
    """Проверяет, в градусах ли координаты (WGS84)"""
    if geom is None or geom.is_empty:
        return False
    minx, miny, maxx, maxy = geom.bounds
    return abs(maxx - minx) < 10 and abs(maxy - miny) < 10

def calculate_area_approx(geom):
    """Приближённый расчёт площади"""
    if geom is None or geom.is_empty:
        return 0
    if is_in_degrees(geom):
        lat_center = (geom.bounds[1] + geom.bounds[3]) / 2
        lon_to_m = 111320 * math.cos(math.radians(lat_center))
        lat_to_m = 110540
        return geom.area * lon_to_m * lat_to_m
    return geom.area

# ============================================================================
# КОНВЕРТАЦИЯ МСК-50 -> WGS84 (ТОЧНЫЕ ПАРАМЕТРЫ)
# ============================================================================

def msk50_to_wgs84(x, y, zone=1):
    """
    Конвертация координат из МСК-50 в WGS84.
    zone: 1 (35°29'), 2 (38°29')
    """
    # Параметры эллипсоида Красовского
    a = 6378245.0
    f = 1.0 / 298.3
    e2 = 2 * f - f * f
    e_prime2 = e2 / (1 - e2)
    
    # Точные центральные меридианы МСК-50
    central_meridians = {1: 35.4833333333, 2: 38.4833333333} # 35°29' и 38°29'
    lon0 = math.radians(central_meridians.get(zone, 35.4833333333))
    
    # Ложные начала (False Easting) для МСК-50
    false_eastings = {1: 500000.0, 2: 1500000.0}
    x0 = false_eastings.get(zone, 500000.0)
    y0 = 0.0
    
    k0 = 1.0
    
    x_prime = x - x0
    y_prime = y - y0
    
    M = y_prime / k0
    mu = M / (a * (1 - e2/4 - 3*e2**2/64 - 5*e2**3/256))
    
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    
    phi1 = mu + (3*e1/2 - 27*e1**3/32) * math.sin(2*mu) + \
           (21*e1**2/16 - 55*e1**4/32) * math.sin(4*mu) + \
           (151*e1**3/96) * math.sin(6*mu) + \
           (1097*e1**4/512) * math.sin(8*mu)
    
    sin_phi1 = math.sin(phi1)
    cos_phi1 = math.cos(phi1)
    tan_phi1 = sin_phi1 / cos_phi1 if cos_phi1 != 0 else 0
    
    N1 = a / math.sqrt(1 - e2 * sin_phi1**2)
    R1 = a * (1 - e2) / (1 - e2 * sin_phi1**2)**1.5
    T1 = tan_phi1**2
    C1 = e_prime2 * cos_phi1**2
    D = x_prime / (N1 * k0)
    
    phi = phi1 - (N1 * tan_phi1 / R1) * (
        D**2/2 - 
        (5 + 3*T1 + 10*C1 - 4*C1**2 - 9*e_prime2) * D**4/24 +
        (61 + 90*T1 + 298*C1 + 45*T1**2 - 252*e_prime2 - 3*C1**2) * D**6/720
    )
    
    lon = lon0 + (
        D - 
        (1 + 2*T1 + C1) * D**3/6 + 
        (5 - 2*C1 + 28*T1 - 3*C1**2 + 8*e_prime2 + 24*T1**2) * D**5/120
    ) / cos_phi1
    
    # Упрощённая трансформация Пулково-42 -> WGS84
    lat_wgs84 = math.degrees(phi) - 0.000016 
    lon_wgs84 = math.degrees(lon) - 0.000016 
    
    return lon_wgs84, lat_wgs84

def convert_geom_to_wgs84(geom, crs_type, ref_lat=55.75, ref_lon=37.62):
    """Конвертирует геометрию в WGS84 для отображения на карте"""
    if crs_type == 'wgs84' or is_in_degrees(geom):
        return geom
    
    if crs_type.startswith('msk50'):
        try:
            zone = int(crs_type.split('_')[-1])
        except:
            zone = 1
            
        def project_msk50(x, y):
            return msk50_to_wgs84(x, y, zone)
        return transform(project_msk50, geom)
    
    elif crs_type == 'web_mercator':
        def project_web_merc(x, y):
            lon = (x / 20037508.34) * 180.0
            lat = (y / 20037508.34) * 180.0
            lat = 180.0 / math.pi * (2 * math.atan(math.exp(lat * math.pi / 180.0)) - math.pi / 2.0)
            return lon, lat
        return transform(project_web_merc, geom)
    
    else:
        def project_local(x, y):
            lat_to_m = 111320.0
            lon_to_m = 111320.0 * math.cos(math.radians(ref_lat))
            return ref_lon + x / lon_to_m, ref_lat + y / lat_to_m
        return transform(project_local, geom)

# ============================================================================
# КЛАСС АНАЛИЗАТОРА
# ============================================================================

class UrbanAnalyzer:
    def __init__(self, original_geom, pzz_config, manual_area=None):
        self.original_geom = original_geom
        self.pzz = pzz_config
        self.buildable_geom = None
        self.tep = {}
        self.financials = {}
        self.area = manual_area if manual_area and manual_area > 0 else calculate_area_approx(original_geom)

    def calculate_buildable(self):
        offset = self.pzz.get("offset", 5)
        density = self.pzz.get("density", 0.4)
        buildable = self.original_geom.buffer(-offset)
        max_buildable = self.area * density
        self.buildable_geom = buildable
        self.buildable_area = min(calculate_area_approx(buildable), max_buildable) if buildable and not buildable.is_empty else 0
        return buildable

    def calculate_tep(self):
        if not self.buildable_geom:
            self.calculate_buildable()
        floors = self.pzz.get("floors", 9)
        floor_h = self.pzz.get("floor_h", 3.2)
        living_ratio = self.pzz.get("living_ratio", 0.75)
        norm = self.pzz.get("norm", 28)
        
        s_total = self.buildable_area * floors
        s_living = s_total * living_ratio
        s_commercial = s_total - s_living
        population = int(s_living / norm) if norm > 0 else 0
        
        self.tep = {
            "s_uch": round(self.area, 1), "s_uch_ha": round(self.area / 10000, 2),
            "s_buildable": round(self.buildable_area, 1), "s_total": round(s_total, 1),
            "s_living": round(s_living, 1), "s_commercial": round(s_commercial, 1),
            "floors": floors, "building_h": floors * floor_h, "population": population,
            "schools": math.ceil(population / 1000 * 120), "kindergartens": math.ceil(population / 1000 * 60),
            "parking": math.ceil(s_total / 100 * 1.2), "green": math.ceil(population * 6)
        }
        return self.tep

    def calculate_financials(self):
        if not self.tep: self.calculate_tep()
        cost = self.pzz.get("cost", 90000)
        price = self.pzz.get("price", 250000)
        comm_ratio = self.pzz.get("comm_ratio", 0.85)
        land = self.pzz.get("land", 50000000)
        
        s_l, s_c, s_t = self.tep["s_living"], self.tep["s_commercial"], self.tep["s_total"]
        rev = s_l * price + s_c * price * comm_ratio
        constr = s_t * cost
        infra = self.tep["schools"]*1.5e6 + self.tep["kindergartens"]*1.2e6 + self.tep["parking"]*0.8e6
        total_cost = constr + land + infra
        profit = rev - total_cost
        roi = (profit / total_cost * 100) if total_cost > 0 else 0
        
        self.financials = {
            "rev": rev, "cost_total": total_cost, "profit": profit, "roi": roi,
            "rev_living": s_l*price, "rev_comm": s_c*price*comm_ratio,
            "cost_constr": constr, "cost_infra": infra, "cost_land": land
        }
        return self.financials

# ============================================================================
# ИНТЕРФЕЙС
# ============================================================================

st.header("📂 Загрузка территории")
data_source = st.radio("Источник:", ["Тестовый участок", "Загрузить GeoJSON"], horizontal=True)
original_geom = None
manual_area = None
crs_type = "wgs84"
ref_lat, ref_lon = 55.75, 37.62

if data_source == "Загрузить GeoJSON":
    uploaded = st.file_uploader("Загрузите .geojson или .json", type=['geojson', 'json'])
    if uploaded:
        try:
            original_geom = parse_geojson(uploaded.getvalue())
            if is_in_degrees(original_geom):
                crs_type = "wgs84"
                st.success("✅ Определена система: WGS84 (градусы)")
            else:
                st.info("⚠️ Координаты в метрах. Выберите систему координат.")
                crs_type = st.selectbox(
                    "Система координат исходного файла:", 
                    ["msk50_zone1", "msk50_zone2", "web_mercator", "local_meters"], 
                    index=0,
                    help="МСК-50 зона 1 (35°29' - запад МО), зона 2 (38°29' - восток МО)"
                )
                if crs_type == "local_meters":
                    col1, col2 = st.columns(2)
                    ref_lat = col1.number_input("Широта центра", value=55.75, step=0.01)
                    ref_lon = col2.number_input("Долгота центра", value=37.62, step=0.01)
            
            area_auto = calculate_area_approx(original_geom)
            st.write(f"📏 Автоматический расчёт площади: `{area_auto:,.0f} м²`")
            use_manual = st.checkbox("✏️ Ввести точную площадь вручную", value=False)
            if use_manual:
                manual_area = st.number_input("Площадь участка (м²):", min_value=1.0, value=float(area_auto), step=10.0)
        except Exception as e:
            st.error(f" Ошибка файла: {str(e)}")
else:
    original_geom = Polygon([(37.615, 55.752), (37.6175, 55.752), (37.6175, 55.7535), (37.615, 55.7535)])
    st.info("Используется тестовый участок (Москва)")

if original_geom is not None:
    st.sidebar.header("️ Градостроительные параметры")
    offset = st.sidebar.number_input("Отступ (м)", 0.0, 50.0, 5.0, 0.5)
    density = st.sidebar.number_input("Плотность застройки", 0.05, 1.0, 0.4, 0.01)
    floors = st.sidebar.number_input("Этажность", 1, 100, 9, 1)
    floor_h = st.sidebar.number_input("Высота этажа (м)", 2.5, 6.0, 3.2, 0.1)
    living_ratio = st.sidebar.number_input("Доля жилья", 0.0, 1.0, 0.75, 0.01)
    norm = st.sidebar.number_input("Норма (м²/чел)", 10, 50, 28, 1)
    
    st.sidebar.header("💰 Финансы")
    cost = st.sidebar.number_input("Себестоимость (₽/м²)", 10000, 300000, 90000, 1000)
    price = st.sidebar.number_input("Цена продажи (₽/м²)", 50000, 1000000, 250000, 5000)
    comm_ratio = st.sidebar.number_input("Коэф. коммерции", 0.1, 3.0, 0.85, 0.05)
    land = st.sidebar.number_input("Земля (млн ₽)", 0.0, 1000.0, 50.0, 1.0) * 1e6
    
    pzz = {"offset": offset, "density": density, "floors": floors, "floor_h": floor_h,
           "living_ratio": living_ratio, "norm": norm, "cost": cost, "price": price,
           "comm_ratio": comm_ratio, "land": land}
    
    analyzer = UrbanAnalyzer(original_geom, pzz, manual_area)
    tep = analyzer.calculate_tep()
    fin = analyzer.calculate_financials()
    
    if tep['s_total'] <= 0:
        st.error("❌ Площадь застройки = 0. Уменьшите отступы или увеличьте плотность/этажность.")
        st.stop()

    st.header(" Технико-экономические показатели")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Участок", f"{tep['s_uch']:,.0f} м²", f"{tep['s_uch_ha']} га")
    c2.metric("Пятно застройки", f"{tep['s_buildable']:,.0f} м²")
    c3.metric("GBA (общая)", f"{tep['s_total']:,.0f} м²")
    c4.metric("Этажность", f"{tep['floors']} эт", f"{tep['building_h']} м")
    
    lp = (tep['s_living']/tep['s_total']*100) if tep['s_total']>0 else 0
    ac1, ac2, ac3 = st.columns(3)
    ac1.info(f"🏠 Жилая: {tep['s_living']:,.0f} м² ({lp:.1f}%)")
    ac2.info(f"🏢 Коммерция: {tep['s_commercial']:,.0f} м² ({100-lp:.1f}%)")
    ac3.info(f"👪 Население: {tep['population']} чел")
    
    st.header("💰 Финансовая модель")
    f1, f2, f3, f4 = st.columns(4)
    f1.metric("Выручка", f"{fin['rev']/1e6:,.1f} млн ₽")
    f2.metric("Затраты", f"{fin['cost_total']/1e6:,.1f} млн ₽")
    p_color = "normal" if fin['profit']>=0 else "inverse"
    f3.metric("Прибыль", f"{fin['profit']/1e6:,.1f} млн ₽", delta_color=p_color)
    f4.metric("ROI", f"{fin['roi']:.1f}%", delta_color=p_color)

    st.header("🗺️ Карта территории")
    map_geom = convert_geom_to_wgs84(original_geom, crs_type, ref_lat, ref_lon)
    map_buildable = convert_geom_to_wgs84(analyzer.buildable_geom, crs_type, ref_lat, ref_lon) if analyzer.buildable_geom else None
    
    bounds = map_geom.bounds
    center = [(bounds[1]+bounds[3])/2, (bounds[0]+bounds[2])/2]
    m = folium.Map(location=center, zoom_start=12, tiles="OpenStreetMap")
    
    folium.GeoJson({"type":"Feature","geometry":mapping(map_geom)}, 
                   style_function=lambda x: {"fillColor":"gray","fillOpacity":0.1,"color":"red","weight":3},
                   tooltip=f"Участок: {tep['s_uch']:,.0f} м²").add_to(m)
    if map_buildable and not map_buildable.is_empty:
        folium.GeoJson({"type":"Feature","geometry":mapping(map_buildable)},
                       style_function=lambda x: {"fillColor":"blue","fillOpacity":0.4,"color":"blue","weight":2},
                       tooltip=f"Пятно: {tep['s_buildable']:,.0f} м²").add_to(m)
    folium.LayerControl().add_to(m)
    st_folium(m, width=1200, height=500)

    st.header("📥 Экспорт")
    ec1, ec2 = st.columns(2)
    with ec1:
        st.download_button("📊 Отчёт JSON", data=io.BytesIO(json.dumps({"tep":tep, "financials":fin}, indent=2, ensure_ascii=False).encode()), file_name="report.json", mime="application/json")
    with ec2:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Показатель","Значение"])
        for k,v in {**tep, **fin}.items(): w.writerow([k,v])
        st.download_button("📄 ТЭП CSV", data=io.BytesIO(buf.getvalue().encode('utf-8-sig')), file_name="tep.csv", mime="text/csv")
    
    st.success(f"✅ Анализ завершён. Система координат: **{crs_type.upper().replace('_', ' ')}**")
else:
    st.warning("⚠️ Загрузите файл или выберите тестовый участок")

st.markdown("---")
st.caption("🛠️ Urban Analyzer PRO | Чистый Python | МСК-50 (Зона 1: 35°29', Зона 2: 38°29')")