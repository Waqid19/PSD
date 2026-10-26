from pathlib import Path
import tempfile
import warnings
import numpy as np
import pandas as pd
import streamlit as st
import rasterio
import geopandas as gpd
import matplotlib.pyplot as plt
from rasterio.warp import reproject, Resampling, transform_bounds
from rasterio.features import shapes
from shapely.geometry import shape
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.ensemble import RandomForestClassifier
from sklearn.svm import SVC
from sklearn.tree import DecisionTreeClassifier
from sklearn.metrics import accuracy_score, f1_score, classification_report, confusion_matrix, ConfusionMatrixDisplay
import folium
from folium import raster_layers
from streamlit_folium import st_folium

warnings.filterwarnings('ignore')
st.set_page_config(page_title='Klasifikasi Tutupan Lahan Jawa Timur', page_icon='🌏', layout='wide')

CLASS_ORDER = ['Hutan', 'Perairan', 'Lahan pertanian', 'Bangunan', 'Mangrove']
CLASS_TO_ID = {name: i + 1 for i, name in enumerate(CLASS_ORDER)}
COLORS = {'Hutan':'#246b32', 'Perairan':'#2878b5', 'Lahan pertanian':'#e6c84f', 'Bangunan':'#d94841', 'Mangrove':'#17a6a1'}
BAND_INFO = [('B2','Band B2 / B02'), ('B3','Band B3 / B03'), ('B4','Band B4 / B04'), ('B5','Band B5 / B05'), ('B8','Band B8 / B08'), ('B8A','Band B8A'), ('B11','Band B11')]

st.title('🌏 Klasifikasi Tutupan Lahan Jawa Timur')
st.write('Aplikasi klasifikasi citra Sentinel-2 menjadi lima kelas menggunakan band spektral dan indeks NDVI, NDMI, MVI, MNDWI, NDRE, EVI, serta REMI.')
st.info('Unggah tujuh raster band dan GeoJSON sampel yang memiliki atribut kelas. Model dilatih dari sampel yang diunggah; hasil bergantung pada kualitas sampel, citra, dan skala reflektansi.')

with st.sidebar:
    st.header('1. Data masukan')
    uploaded_bands = {}
    for key, label in BAND_INFO:
        uploaded_bands[key] = st.file_uploader(label, type=['tif','tiff'], key=f'band_{key}')
    sample_file = st.file_uploader('GeoJSON sampel berlabel', type=['geojson','json'], key='samples')
    label_column = st.text_input('Nama kolom label (kosong = deteksi otomatis)', value='')
    model_choice = st.selectbox('Model untuk peta', ['Random Forest', 'SVM', 'Decision Tree'])
    st.caption('Gunakan data dari tanggal/area yang sama dan pastikan semua raster memiliki cakupan yang sesuai.')


def safe_divide(a, b):
    out = np.full(a.shape, np.nan, dtype='float32')
    ok = np.isfinite(a) & np.isfinite(b) & (np.abs(b) > 1e-8)
    out[ok] = a[ok] / b[ok]
    return out


def normalize_class(value):
    v = str(value).strip().lower().replace('_', ' ').replace('-', ' ')
    if 'mangrove' in v: return 'Mangrove'
    if 'hutan' in v or 'forest' in v or v == 'wood': return 'Hutan'
    if any(x in v for x in ['perair', 'water', 'laut', 'danau', 'sungai']): return 'Perairan'
    if any(x in v for x in ['pertanian', 'sawah', 'farm', 'agri', 'kebun']): return 'Lahan pertanian'
    if any(x in v for x in ['bangunan', 'building', 'built', 'permukiman']): return 'Bangunan'
    return None


def read_uploaded_raster(uploaded):
    # rasterio memerlukan path; file upload sementara disimpan dengan ekstensi GeoTIFF.
    suffix = '.tif' if uploaded.name.lower().endswith('.tif') else '.tiff'
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(uploaded.getvalue()); tmp.flush(); tmp.close()
    return tmp.name


def make_features(bands):
    b2,b3,b4,b5,b8,b8a,b11 = (bands[k] for k in ['B2','B3','B4','B5','B8','B8A','B11'])
    feats = {f'REF_{k}': bands[k] for k in bands}
    feats['NDVI'] = safe_divide(b8-b4, b8+b4)
    feats['NDMI'] = safe_divide(b8-b11, b8+b11)
    feats['MVI'] = safe_divide(b8-b3, b11-b3)
    feats['MNDWI'] = safe_divide(b3-b11, b3+b11)
    feats['NDRE_B8_B5'] = safe_divide(b8-b5, b8+b5)
    feats['EVI'] = safe_divide(2.5*(b8-b4), b8+6*b4-7.5*b2+1)
    feats['REMI'] = safe_divide(b5-b4, b11-b3)
    return feats


def to_rgb_rgba(class_map):
    rgba = np.zeros((*class_map.shape, 4), dtype=np.uint8)
    for name, cid in CLASS_TO_ID.items():
        hex_color = COLORS[name].lstrip('#')
        rgb = tuple(int(hex_color[i:i+2], 16) for i in (0,2,4))
        mask = class_map == cid
        rgba[mask, :3] = rgb
        rgba[mask, 3] = 220
    return rgba

ready = all(uploaded_bands[k] is not None for k, _ in BAND_INFO) and sample_file is not None
if not ready:
    st.subheader('Cara menggunakan')
    st.markdown("""1. Unggah raster **B2, B3, B4, B5, B8, B8A, dan B11** di panel kiri.\n2. Unggah GeoJSON sampel yang memiliki kolom kelas, misalnya `class_name`, `kelas`, `type`, atau `label`.\n3. Pilih model dan klik **Jalankan klasifikasi**.\n4. Unduh hasil raster GeoTIFF atau tabel evaluasi.""")
    st.stop()

if st.button('Jalankan klasifikasi', type='primary', use_container_width=True):
    try:
        with st.spinner('Membaca data dan menyelaraskan raster...'):
            raster_paths = {k: read_uploaded_raster(v) for k,v in uploaded_bands.items()}
            with rasterio.open(raster_paths['B2']) as ref:
                profile = ref.profile.copy(); transform = ref.transform; crs = ref.crs
                height, width = ref.height, ref.width; bounds = ref.bounds
            bands = {}
            for key, path in raster_paths.items():
                with rasterio.open(path) as src:
                    arr = np.full((height,width), np.nan, dtype='float32')
                    reproject(source=rasterio.band(src,1), destination=arr,
                              src_transform=src.transform, src_crs=src.crs, src_nodata=src.nodata,
                              dst_transform=transform, dst_crs=crs, dst_nodata=np.nan,
                              resampling=Resampling.bilinear)
                    bands[key] = arr
            samples = gpd.read_file(sample_file)
            if samples.empty: raise ValueError('GeoJSON sampel kosong.')
            col = label_column.strip() or next((c for c in ['class_name','kelas','class','label','type','nama_kelas'] if c in samples.columns), None)
            if not col or col not in samples.columns:
                raise ValueError('Kolom label tidak ditemukan. Isi nama kolom label di panel kiri.')
            if samples.crs is None: raise ValueError('CRS GeoJSON tidak tersedia. Tetapkan CRS sampel terlebih dahulu.')
            samples = samples[samples.geometry.notna() & ~samples.geometry.is_empty].copy().to_crs(crs)
            samples['class_name'] = samples[col].map(normalize_class)
            samples = samples[samples['class_name'].isin(CLASS_ORDER)].copy()
            if samples.empty: raise ValueError('Label tidak cocok dengan lima kelas: Hutan, Perairan, Lahan pertanian, Bangunan, Mangrove.')
            features = make_features(bands); feature_names = list(features)

        with st.spinner('Mengekstrak fitur dan melatih model...'):
            rows=[]
            pts = samples.geometry.representative_point()
            for idx, point in zip(samples.index, pts):
                row, col_px = rasterio.transform.rowcol(transform, point.x, point.y)
                rec={'class_name': samples.loc[idx,'class_name']}
                for name, arr in features.items():
                    rec[name] = arr[row,col_px] if 0 <= row < height and 0 <= col_px < width else np.nan
                rows.append(rec)
            training = pd.DataFrame(rows).replace([np.inf,-np.inf],np.nan).dropna(subset=feature_names+['class_name'])
            counts = training['class_name'].value_counts()
            missing = [c for c in CLASS_ORDER if c not in counts.index]
            if missing: raise ValueError('Sampel valid tidak mencakup semua kelas: '+', '.join(missing)+'. Periksa posisi sampel, CRS, dan NoData.')
            if counts.min() < 2: raise ValueError('Setiap kelas memerlukan minimal dua sampel valid untuk pembagian data.')
            X=training[feature_names].astype('float32'); y=training['class_name']
            X_train,X_test,y_train,y_test=train_test_split(X,y,test_size=0.2,random_state=42,stratify=y)
            models={
                'Random Forest':RandomForestClassifier(n_estimators=300,class_weight='balanced',random_state=42,n_jobs=-1),
                'SVM':Pipeline([('scaler',StandardScaler()),('model',SVC(C=3,kernel='rbf',class_weight='balanced'))]),
                'Decision Tree':DecisionTreeClassifier(class_weight='balanced',random_state=42,min_samples_leaf=2)
            }
            fitted={}; rows_metrics=[]
            for name, model in models.items():
                model.fit(X_train,y_train); pred=model.predict(X_test); fitted[name]=model
                rows_metrics.append({'Model':name,'Accuracy (%)':accuracy_score(y_test,pred)*100,
                                     'Macro F1 (%)':f1_score(y_test,pred,average='macro',zero_division=0)*100,
                                     'Weighted F1 (%)':f1_score(y_test,pred,average='weighted',zero_division=0)*100})
            metrics=pd.DataFrame(rows_metrics).sort_values('Macro F1 (%)',ascending=False).reset_index(drop=True)
            model=fitted[model_choice]
            pred_test=model.predict(X_test)
            report=pd.DataFrame(classification_report(y_test,pred_test,labels=CLASS_ORDER,output_dict=True,zero_division=0)).T

        with st.spinner('Memprediksi raster...'):
            stack=np.stack([features[n] for n in feature_names],axis=-1)
            class_map=np.zeros((height,width),dtype=np.uint8)
            for start in range(0,height,128):
                end=min(start+128,height); block=stack[start:end]
                mask=np.all(np.isfinite(block),axis=-1)
                if mask.any():
                    labels=model.predict(block[mask].reshape(-1,len(feature_names)))
                    class_map[start:end,:][mask]=np.array([CLASS_TO_ID[x] for x in labels],dtype=np.uint8)
            st.session_state['result']={
                'class_map':class_map,'profile':profile,'transform':transform,'crs':crs,'bounds':bounds,
                'metrics':metrics,'report':report,'model_choice':model_choice,'training_count':len(training),
                'sample_counts':counts.to_dict(),'features':feature_names,'height':height,'width':width,
                'y_test':y_test.tolist(),'pred_test':pred_test.tolist()
            }
        st.success('Klasifikasi selesai. Lihat hasil di bawah.')
    except Exception as e:
        st.error(f'Proses gagal: {e}')

if 'result' in st.session_state:
    r=st.session_state['result']; class_map=r['class_map']
    st.header('Hasil klasifikasi')
    c1,c2,c3=st.columns(3)
    c1.metric('Sampel valid',r['training_count'])
    c2.metric('Model peta',r['model_choice'])
    c3.metric('Ukuran raster',f"{r['width']} × {r['height']}")
    st.subheader('Perbandingan model')
    st.dataframe(r['metrics'].round(2),use_container_width=True,hide_index=True)
    st.subheader('Metrik per kelas')
    st.dataframe(r['report'].round(3),use_container_width=True)
    st.caption('Metrik berasal dari pembagian acak 80:20 atas sampel yang diekstrak. Ini bukan validasi spasial independen.')

    st.subheader('Peta klasifikasi')
    fig,ax=plt.subplots(figsize=(12,7))
    from matplotlib.colors import ListedColormap, BoundaryNorm
    from matplotlib.patches import Patch
    cmap=ListedColormap(['#ffffff']+[COLORS[x] for x in CLASS_ORDER])
    norm=BoundaryNorm([-0.5,0.5,1.5,2.5,3.5,4.5,5.5],cmap.N)
    ax.imshow(class_map,cmap=cmap,norm=norm); ax.axis('off'); ax.set_title(f"Tutupan Lahan — {r['model_choice']}")
    ax.legend(handles=[Patch(facecolor=COLORS[x],label=x) for x in CLASS_ORDER],loc='lower left',ncol=2)
    st.pyplot(fig); plt.close(fig)

    st.subheader('Peta interaktif')
    try:
        b=transform_bounds(r['crs'],'EPSG:4326',*r['bounds'])
        map_bounds=[[b[1],b[0]],[b[3],b[2]]]
        center=[(b[1]+b[3])/2,(b[0]+b[2])/2]
        m=folium.Map(location=center,zoom_start=8,tiles='OpenStreetMap')
        rgba=to_rgb_rgba(class_map)
        raster_layers.ImageOverlay(image=rgba,bounds=map_bounds,opacity=0.8,interactive=True,name='Klasifikasi').add_to(m)
        folium.LayerControl().add_to(m)
        st_folium(m,use_container_width=True,height=520,returned_objects=[])
    except Exception as e:
        st.warning(f'Peta interaktif tidak dapat ditampilkan: {e}')

    st.subheader('Unduh hasil')
    import io
    tif_buffer=io.BytesIO()
    profile=r['profile'].copy(); profile.update(dtype='uint8',count=1,nodata=0,compress='lzw')
    with rasterio.MemoryFile() as mem:
        with mem.open(**profile) as dst: dst.write(class_map,1)
        tif_bytes=mem.read()
    st.download_button('Unduh hasil GeoTIFF',data=tif_bytes,file_name='klasifikasi_tutupan_lahan_5_kelas.tif',mime='image/tiff')
    st.download_button('Unduh evaluasi model CSV',data=r['metrics'].to_csv(index=False).encode('utf-8-sig'),file_name='evaluasi_model.csv',mime='text/csv')
    st.download_button('Unduh laporan per kelas CSV',data=r['report'].to_csv().encode('utf-8-sig'),file_name='laporan_per_kelas.csv',mime='text/csv')
    st.download_button('Unduh sampel valid CSV',data=pd.DataFrame({'kelas':r['y_test'],'prediksi':r['pred_test']}).to_csv(index=False).encode('utf-8-sig'),file_name='prediksi_data_uji.csv',mime='text/csv')
