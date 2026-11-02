import streamlit as st
import requests
import os
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import date
from dotenv import load_dotenv
from openai import OpenAI
from garminconnect import Garmin

# ==========================================
# 1. CONFIGURAZIONE AMBIENTE E SICUREZZA
# ==========================================
load_dotenv()
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")
openai_api_key = os.getenv("OPENAI_API_KEY")
garmin_email = os.getenv("GARMIN_EMAIL")
garmin_password = os.getenv("GARMIN_PASSWORD")

client = OpenAI(api_key=openai_api_key)

st.set_page_config(page_title="Protocollo I90 | Sistema operativo ibrido", page_icon="🐺", layout="wide")

# ==========================================
# 2. FUNZIONI DI SALVATAGGIO E SINCRONIZZAZIONE
# ==========================================
def salva_dato_i90_giornaliero(chiave_json, payload_specifico):
    """Salva i dati I90 dentro raw_data preservando i dati Garmin."""
    oggi_str = date.today().isoformat()
    headers_get = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}
    headers_upsert = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates"
    }
    
    raw_attuale = {}
    res = requests.get(f"{supabase_url}/rest/v1/metrica_giornaliera?data=eq.{oggi_str}&select=raw_data", headers=headers_get)
    if res.status_code == 200 and len(res.json()) > 0:
        dato_letto = res.json()[0].get("raw_data")
        if isinstance(dato_letto, dict):
            raw_attuale = dato_letto
            
    raw_attuale[chiave_json] = payload_specifico
    requests.post(
        f"{supabase_url}/rest/v1/metrica_giornaliera",
        headers=headers_upsert,
        json={"data": oggi_str, "raw_data": raw_attuale}
    ).raise_for_status()


def sincronizza_garmin_completo():
    try:
        garmin = Garmin(garmin_email, garmin_password)
        garmin.login()
        oggi = date.today().isoformat()
        
        headers_get = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}
        headers_upsert = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates"
        }

        chiavi_i90_salvate = {}
        res_esistente = requests.get(f"{supabase_url}/rest/v1/metrica_giornaliera?data=eq.{oggi}&select=raw_data", headers=headers_get)
        if res_esistente.status_code == 200 and len(res_esistente.json()) > 0:
            raw_esistente = res_esistente.json()[0].get("raw_data")
            if isinstance(raw_esistente, dict):
                for k in ["i90_mindset", "i90_benchmarks", "i90_workout_log", "i90_stato"]:
                    if k in raw_esistente:
                        chiavi_i90_salvate[k] = raw_esistente[k]

        stats = garmin.get_stats(oggi)
        if not isinstance(stats, dict):
            stats = {}
        for k, v in chiavi_i90_salvate.items():
            stats[k] = v

        sonno = garmin.get_sleep_data(oggi)
        
        bb_attuale = stats.get('bodyBatteryHighestValue')
        try:
            bb_data = garmin.get_body_battery(oggi)
            if bb_data and isinstance(bb_data, list) and len(bb_data) > 0:
                valori = bb_data[0].get('bodyBatteryValuesArray', [])
                for misurazione in reversed(valori):
                    if len(misurazione) >= 2 and misurazione[1] is not None:
                        bb_attuale = int(misurazione[1])
                        break
        except Exception:
            pass
        
        sonno_secondi = sonno.get('dailySleepDTO', {}).get('sleepTimeSeconds', 0)
        sonno_ore = round(sonno_secondi / 3600, 2) if sonno_secondi else None
        sonno_punteggio = sonno.get('dailySleepDTO', {}).get('sleepScores', {}).get('overall', {}).get('value')
        
        payload_giornaliero = {
            "data": oggi,
            "sonno_ore": sonno_ore,
            "sonno_punteggio": sonno_punteggio,
            "hrv_status": stats.get('hrvStatus'),
            "rhr": stats.get('restingHeartRate'),
            "body_battery_max": stats.get('bodyBatteryHighestValue'),
            "body_battery_min": stats.get('bodyBatteryLowestValue'),
            "body_battery_attuale": bb_attuale,
            "stress_medio": stats.get('averageStressLevel'),
            "passi": stats.get('steps'),
            "raw_data": stats
        }
        requests.post(f"{supabase_url}/rest/v1/metrica_giornaliera", headers=headers_upsert, json=payload_giornaliero).raise_for_status()

        activities = garmin.get_activities(0, 15) 
        for act in activities:
            act_date = act.get('startTimeLocal', '').split(' ')[0]
            if not act_date:
                continue
            
            payload_act = {
                "data": act_date,
                "attivita_id_garmin": str(act.get('activityId')),
                "tipo_sport": str(act.get('activityType', {}).get('typeKey', 'sconosciuto')),
                "raw_data": act
            }
            
            if act.get('duration'):
                payload_act["durata_minuti"] = round(float(act['duration']) / 60, 2)
            if act.get('averageHR'):
                payload_act["fc_media"] = int(act['averageHR'])
            if act.get('maxHR'):
                payload_act["fc_max"] = int(act['maxHR'])
            if act.get('aerobicTrainingEffect'):
                payload_act["training_effect_aerobico"] = float(act['aerobicTrainingEffect'])
            if act.get('anaerobicTrainingEffect'):
                payload_act["training_effect_anaerobico"] = float(act['anaerobicTrainingEffect'])
            
            t_load = act.get('trainingLoad') or act.get('vO2MaxValue')
            if t_load:
                payload_act["training_load"] = float(t_load)
            
            try:
                res = requests.post(f"{supabase_url}/rest/v1/attivita_sportive", headers=headers_upsert, json=payload_act)
                res.raise_for_status()
            except requests.exceptions.HTTPError:
                pass 
        return True
    except Exception as e:
        st.error(f"Errore critico Garmin: {e}")
        return False

# ==========================================
# 3. MOTORE DI LETTURA DATI ED ESTRAZIONE STORICO I90
# ==========================================
@st.cache_data(ttl=60)
def fetch_data():
    headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}
    
    res_garmin = requests.get(f"{supabase_url}/rest/v1/metrica_giornaliera?select=*", headers=headers)
    df_garmin = pd.DataFrame(res_garmin.json()) if res_garmin.status_code == 200 else pd.DataFrame()

    res_zepp = requests.get(f"{supabase_url}/rest/v1/composizione_corporea?select=*", headers=headers)
    df_zepp = pd.DataFrame(res_zepp.json()) if res_zepp.status_code == 200 else pd.DataFrame()

    res_attivita = requests.get(f"{supabase_url}/rest/v1/attivita_sportive?select=*&order=data.desc", headers=headers)
    df_attivita = pd.DataFrame(res_attivita.json()) if res_attivita.status_code == 200 else pd.DataFrame()

    df_completo = pd.DataFrame()
    if not df_garmin.empty and not df_zepp.empty:
        df_completo = pd.merge(df_garmin, df_zepp, on='data', how='outer')
        df_completo['data'] = pd.to_datetime(df_completo['data'])
        df_completo = df_completo.sort_values('data')
        df_completo = df_completo.ffill()
        df_completo['massa_grassa_kg'] = df_completo['peso_kg'] * (df_completo['massa_grassa_perc'] / 100)
        df_completo['acqua_kg'] = df_completo['peso_kg'] * (df_completo['acqua_perc'] / 100)
    elif not df_garmin.empty:
        df_completo = df_garmin.copy()
        df_completo['data'] = pd.to_datetime(df_completo['data'])
        df_completo = df_completo.sort_values('data')
        
    return df_completo, df_attivita

df_master, df_attivita = fetch_data()

lista_benchmarks = []
lista_workout_logs = []
ultimo_stato_i90 = {"ciclo": 1, "settimana": 1}

if not df_master.empty and "raw_data" in df_master.columns:
    for _, riga in df_master.iterrows():
        raw_d = riga.get("raw_data")
        data_riga = riga.get("data")
        if isinstance(raw_d, dict):
            if "i90_stato" in raw_d and isinstance(raw_d["i90_stato"], dict):
                ultimo_stato_i90.update(raw_d["i90_stato"])
            if "i90_benchmarks" in raw_d and isinstance(raw_d["i90_benchmarks"], dict):
                b_item = raw_d["i90_benchmarks"].copy()
                b_item["data"] = data_riga
                lista_benchmarks.append(b_item)
            if "i90_workout_log" in raw_d and isinstance(raw_d["i90_workout_log"], dict):
                w_item = raw_d["i90_workout_log"].copy()
                w_item["data"] = data_riga
                lista_workout_logs.append(w_item)

df_benchmarks = pd.DataFrame(lista_benchmarks)
df_workout_logs = pd.DataFrame(lista_workout_logs)

bench_attuali = {
    "panca_1rm": 80.0,
    "squat_1rm": 100.0,
    "stacco_1rm": 120.0,
    "military_1rm": 50.0,
    "trazioni_max": 10,
    "dip_max": 15,
    "curl_kg": 30.0,
    "passo_5k": "04:50"
}
if not df_benchmarks.empty:
    ultimo_b = df_benchmarks.iloc[-1]
    for k in bench_attuali.keys():
        if k in ultimo_b and pd.notna(ultimo_b[k]):
            bench_attuali[k] = ultimo_b[k]

# Inizializzazione stato per il passaggio dal calcolatore submassimale al form
for k, v in bench_attuali.items():
    if f"input_{k}" not in st.session_state:
        st.session_state[f"input_{k}"] = v

# ==========================================
# 4. BARRA LATERALE: CONTROLLO CICLO E SETTIMANA
# ==========================================
with st.sidebar:
    st.header("⚡ Terminale I90")
    if st.button("🔄 SINCRONIZZA GARMIN", type="primary", use_container_width=True):
        with st.spinner("Estrazione telemetria in corso..."):
            if sincronizza_garmin_completo():
                st.success("Sincronizzazione completata!")
                st.cache_data.clear()
                st.rerun()
                
    st.markdown("---")
    st.subheader("📅 Gestione ciclo 90 giorni")
    
    ciclo_i90 = st.number_input(
        "Ciclo I90 (stagione di 12 settimane)",
        min_value=1, max_value=20,
        value=int(ultimo_stato_i90.get("ciclo", 1)),
        step=1,
        help="Incrementa a 2 quando finisci la settimana 12 per iniziare un nuovo percorso e confrontare i progressi."
    )
    settimana_i90 = st.slider(
        "Settimana del ciclo corrente",
        min_value=1, max_value=12,
        value=int(ultimo_stato_i90.get("settimana", 1))
    )
    
    if st.button("💾 Fissa ciclo e settimana", use_container_width=True):
        try:
            salva_dato_i90_giornaliero("i90_stato", {"ciclo": ciclo_i90, "settimana": settimana_i90})
            st.cache_data.clear()
            st.success(f"Posizione salvata: Ciclo {ciclo_i90}, Settimana {settimana_i90}")
        except Exception as e:
            st.error(f"Errore salvataggio: {e}")

    mappa_parametri_settimana = {
        1:  {"blocco": "Blocco 1: Fondamenta", "scarico": False, "perc_1rm": 0.75,  "serie_fond": 4, "reps_fond": "6-8", "rir": 2, "perc_cal": 0.65, "serie_cal": 4},
        2:  {"blocco": "Blocco 1: Fondamenta", "scarico": False, "perc_1rm": 0.775, "serie_fond": 4, "reps_fond": "6",   "rir": 2, "perc_cal": 0.70, "serie_cal": 4},
        3:  {"blocco": "Blocco 1: Fondamenta", "scarico": False, "perc_1rm": 0.80,  "serie_fond": 5, "reps_fond": "5-6", "rir": 1, "perc_cal": 0.75, "serie_cal": 5},
        4:  {"blocco": "Blocco 1: Fondamenta (Scarico)", "scarico": True, "perc_1rm": 0.70, "serie_fond": 3, "reps_fond": "5", "rir": 3, "perc_cal": 0.50, "serie_cal": 3},
        5:  {"blocco": "Blocco 2: Costruzione", "scarico": False, "perc_1rm": 0.825, "serie_fond": 5, "reps_fond": "5",   "rir": 1, "perc_cal": 0.75, "serie_cal": 5},
        6:  {"blocco": "Blocco 2: Costruzione", "scarico": False, "perc_1rm": 0.85,  "serie_fond": 5, "reps_fond": "4-5", "rir": 1, "perc_cal": 0.80, "serie_cal": 5},
        7:  {"blocco": "Blocco 2: Costruzione", "scarico": False, "perc_1rm": 0.875, "serie_fond": 5, "reps_fond": "4",   "rir": 1, "perc_cal": 0.85, "serie_cal": 5},
        8:  {"blocco": "Blocco 2: Costruzione (Scarico)", "scarico": True, "perc_1rm": 0.725, "serie_fond": 3, "reps_fond": "4", "rir": 3, "perc_cal": 0.55, "serie_cal": 3},
        9:  {"blocco": "Blocco 3: Picco",       "scarico": False, "perc_1rm": 0.875, "serie_fond": 5, "reps_fond": "3-4", "rir": 1, "perc_cal": 0.85, "serie_cal": 6},
        10: {"blocco": "Blocco 3: Picco",       "scarico": False, "perc_1rm": 0.90,  "serie_fond": 5, "reps_fond": "3",   "rir": 1, "perc_cal": 0.90, "serie_cal": 6},
        11: {"blocco": "Blocco 3: Picco",       "scarico": False, "perc_1rm": 0.925, "serie_fond": 5, "reps_fond": "2-3", "rir": 0, "perc_cal": 0.90, "serie_cal": 6},
        12: {"blocco": "Blocco 3: Scarico e test finale", "scarico": True, "perc_1rm": 0.70, "serie_fond": 3, "reps_fond": "3", "rir": 3, "perc_cal": 0.50, "serie_cal": 3}
    }
    
    param_sett = mappa_parametri_settimana[settimana_i90]
    stato_ciclo_txt = "🟢 SCARICO (3:1)" if param_sett["scarico"] else f"🔥 SPINTA ({int(param_sett['perc_1rm']*100)}% massimale)"
    
    def arrotonda_peso(valore):
        return round((valore * param_sett["perc_1rm"]) / 2.5) * 2.5

    kg_panca_target = arrotonda_peso(float(bench_attuali["panca_1rm"]))
    kg_squat_target = arrotonda_peso(float(bench_attuali["squat_1rm"]))
    kg_stacco_target = arrotonda_peso(float(bench_attuali["stacco_1rm"]))
    kg_military_target = arrotonda_peso(float(bench_attuali["military_1rm"]))
    kg_curl_target = round(float(bench_attuali["curl_kg"]) / 2.5) * 2.5
    reps_trazioni_target = max(3, round(int(bench_attuali["trazioni_max"]) * param_sett["perc_cal"]))
    reps_dip_target = max(5, round(int(bench_attuali["dip_max"]) * param_sett["perc_cal"]))

    st.info(
        f"**Ciclo {ciclo_i90} - Settimana {settimana_i90}/12**\n\n"
        f"**{param_sett['blocco']}**\n\n"
        f"Stato: {stato_ciclo_txt}\n\n"
        f"**Carichi di lavoro calcolati oggi:**\n"
        f"- Panca: **{kg_panca_target} kg** ({param_sett['serie_fond']}x{param_sett['reps_fond']})\n"
        f"- Squat: **{kg_squat_target} kg** ({param_sett['serie_fond']}x{param_sett['reps_fond']})\n"
        f"- Stacco: **{kg_stacco_target} kg** ({param_sett['serie_fond']}x{param_sett['reps_fond']})\n"
        f"- Military: **{kg_military_target} kg** ({param_sett['serie_fond']}x{param_sett['reps_fond']})\n"
        f"- Trazioni libere: **{param_sett['serie_cal']}x{reps_trazioni_target} rip.**\n"
        f"- Dip parallele: **{param_sett['serie_cal']}x{reps_dip_target} rip.**\n"
        f"- Curl bicipiti: **4x8-10 a {kg_curl_target} kg**"
    )

# ==========================================
# 5. INTESTAZIONE E SCHEDE DI NAVIGAZIONE
# ==========================================
st.title("🐺 Protocollo I90 - Sistema operativo ibrido")
st.markdown("*Forza progressiva. Resistenza aerobica. Disciplina binaria sulle 24 ore.*")
st.markdown("---")

tab_dashboard, tab_coach, tab_carichi, tab_mindset, tab_inserimento = st.tabs([
    "📊 Telemetria", 
    "⚔️ Allenamento I90", 
    "📈 Massimali e calcolatore",
    "🧠 Le 4 regole", 
    "⚖️ Composizione corporea"
])

# ==========================================
# SCHEDA 1: TELEMETRIA E ANALISI
# ==========================================
with tab_dashboard:
    if df_master.empty:
        st.warning("Nessun dato disponibile nel database. Sincronizza Garmin o inserisci dati corporei.")
    else:
        ultima_riga = df_master.iloc[-1]
        raw = ultima_riga.get('raw_data', {})
        if not isinstance(raw, dict):
            raw = {}
        
        col1, col2, col3, col4 = st.columns(4)
        
        sonno = ultima_riga.get('sonno_ore')
        sonno_val = round(sonno, 1) if pd.notna(sonno) else "N/D"
        col1.metric("Ore di sonno", f"{sonno_val} h")
        
        fc = ultima_riga.get('rhr')
        fc_val = int(fc) if pd.notna(fc) else "N/D"
        col2.metric("FC a riposo", f"{fc_val} bpm")
        
        calorie = raw.get('totalKilocalories')
        cal_val = int(calorie) if pd.notna(calorie) else "N/D"
        col3.metric("Calorie totali", f"{cal_val} kcal")
        
        stress = ultima_riga.get('stress_medio')
        stress_val = int(stress) if pd.notna(stress) else "N/D"
        col4.metric("Stress medio", f"{stress_val} / 100")
        st.markdown("---")

        st.subheader("🏅 Ultima sessione registrata su Garmin")
        if not df_attivita.empty:
            df_attivita['titolo_allenamento'] = df_attivita['raw_data'].apply(
                lambda r: r.get('activityName', 'Generico') if isinstance(r, dict) else 'Generico'
            )
            ultima_att = df_attivita.iloc[0]
            col_a1, col_a2, col_a3, col_a4 = st.columns(4)
            
            tipo = str(ultima_att.get('tipo_sport', 'N/D')).replace('_', ' ').title()
            col_a1.metric("Disciplina", f"{tipo} ({ultima_att.get('titolo_allenamento', '')})")
            
            durata = ultima_att.get('durata_minuti')
            col_a2.metric("Durata", f"{durata} min" if pd.notna(durata) else "N/D")
            
            fc_m = ultima_att.get('fc_media')
            col_a3.metric("FC media", f"{int(fc_m)} bpm" if pd.notna(fc_m) else "N/D")
            
            te_aerobico = ultima_att.get('training_effect_aerobico')
            col_a4.metric("Impatto aerobico", f"{te_aerobico} TE" if pd.notna(te_aerobico) else "N/D")
            
            with st.expander("🎙️ Richiedi analisi tecnica post-allenamento"):
                if st.button("Analizza telemetria ultima sessione", use_container_width=True):
                    with st.spinner("Decriptazione telemetria Garmin in corso..."):
                        act_raw = df_attivita.iloc[0].get('raw_data', {})
                        if not isinstance(act_raw, dict):
                            act_raw = {}
                        
                        distanza_metri = act_raw.get('distance', 0)
                        distanza_km = round(distanza_metri / 1000, 2) if distanza_metri else "N/D"
                        
                        velocita_ms = act_raw.get('averageSpeed', 0)
                        passo_min_km = "N/D"
                        if velocita_ms and velocita_ms > 0:
                            minuti_decimali = 16.6667 / velocita_ms
                            minuti = int(minuti_decimali)
                            secondi = int((minuti_decimali - minuti) * 60)
                            passo_min_km = f"{minuti}:{secondi:02d} min/km"

                        prompt_debriefing = f"""
                        Sei il Direttore Tecnico del Protocollo I90.
                        Analizza la telemetria dell'ultimo allenamento di Francesco (Ciclo {ciclo_i90}, Settimana {settimana_i90}, {param_sett['blocco']}).
                        
                        DATI TELEMETRICI GARMIN:
                        - Disciplina: {tipo} | Titolo: {ultima_att.get('titolo_allenamento', 'Generico')}
                        - Durata: {durata} min | Distanza: {distanza_km} km | Passo medio: {passo_min_km}
                        - FC media: {ultima_att.get('fc_media', 'N/D')} bpm | FC max: {ultima_att.get('fc_max', 'N/D')} bpm
                        - Training Effect aerobico: {te_aerobico} | Anaerobico: {ultima_att.get('training_effect_anaerobico', 'N/D')}
                        
                        Valuta la coerenza fisiologica con il Protocollo I90 (rispetto della Zona 2 nei giorni facili, intensità reale nelle ripetute, densità nei pesi).
                        Usa esattamente questa struttura:
                        - 🎯 **Valutazione esecutiva:** giudizio diretto in 2 righe.
                        - 📈 **Analisi fisiologica:** impatto sul motore aerobico o sul sistema nervoso.
                        - ⚠️ **Direttiva per domani:** gestione del recupero per la prossima seduta I90.
                        """
                        try:
                            risposta_debrief = client.chat.completions.create(
                                model="gpt-4o-mini",
                                messages=[{"role": "system", "content": prompt_debriefing}],
                                temperature=0.5
                            )
                            st.info(risposta_debrief.choices[0].message.content)
                        except Exception as e:
                            st.error(f"Errore API OpenAI: {e}")
        else:
            st.info("Nessuna attività registrata.")

        st.markdown("---")
        col_top1, col_top2 = st.columns(2)
        with col_top1:
            bb_attuale_val = ultima_riga.get('body_battery_attuale')
            if pd.isna(bb_attuale_val):
                bb_attuale_val = ultima_riga.get('body_battery_max', 0)
            fig_gauge = go.Figure(go.Indicator(
                mode="gauge+number", value=bb_attuale_val if pd.notna(bb_attuale_val) else 0,
                title={'text': "Serbatoio energetico (Body Battery)"},
                gauge={
                    'axis': {'range': [0, 100]}, 'bar': {'color': "#3b82f6"},
                    'steps': [{'range': [0, 30], 'color': "#ff4b4b"}, {'range': [30, 70], 'color': "#ffa600"}, {'range': [70, 100], 'color': "#00cc96"}]
                }
            ))
            st.plotly_chart(fig_gauge, use_container_width=True)

        with col_top2:
            if 'muscoli_kg' in ultima_riga:
                comp_data = pd.DataFrame({
                    'Componente': ['Massa muscolare', 'Massa grassa', 'Massa ossea', 'Grasso viscerale', 'Acqua corporea'],
                    'Valori': [
                        ultima_riga.get('muscoli_kg', 0),
                        ultima_riga.get('massa_grassa_kg', 0),
                        ultima_riga.get('massa_ossea_kg', 0),
                        ultima_riga.get('grasso_viscerale', 0),
                        ultima_riga.get('acqua_kg', 0)
                    ]
                }).fillna(0)
                comp_data = comp_data[comp_data['Valori'] > 0]
                if not comp_data.empty:
                    fig_tree = px.treemap(comp_data, path=['Componente'], values='Valori', title="Struttura corporea (kg)", color='Valori', color_continuous_scale='Reds')
                    st.plotly_chart(fig_tree, use_container_width=True)

# ==========================================
# SCHEDA 2: ALLENAMENTO PROTOCOLLO I90 E REGISTRO DI CAMPO
# ==========================================
with tab_coach:
    st.header(f"⚔️ Sessione giornaliera - Ciclo {ciclo_i90}, Settimana {settimana_i90}")
    st.caption("Isolamento dell'interferenza: 3 sedute di forza e 3 di corsa a settimana. Trazioni e dip a corpo libero.")
    
    mappa_giorni_i90 = {
        0: "1. Lunedì: Recupero attivo o Duathlon indoor (Bici + Corsa Z2)",
        1: "2. Martedì: Forza parte superiore + Armatura braccia (Panca, Trazioni, Military, Rematore + Dip e Curl)",
        2: "3. Mercoledì: Corsa ripetute e soglia (Motore anaerobico e VO2 Max)",
        3: "4. Giovedì: Forza parte inferiore (Squat, Stacco da terra, Unilaterali e Core)",
        4: "5. Venerdì: Corsa Easy Run in Zona 2 (Costruzione capillare e recupero)",
        5: "6. Sabato: Condizionamento ibrido (Forza esplosiva + Circuito Metcon stile Hyrox)",
        6: "7. Domenica: Corsa Long Run progressivo (Resistenza aerobica strutturale)"
    }
    
    giorno_odierno_idx = date.today().weekday()
    opzioni_i90 = list(mappa_giorni_i90.values())
    
    col_s1, col_s2 = st.columns([2, 1])
    with col_s1:
        modulo_scelto = st.selectbox(
            "🎯 Modulo giornaliero I90 (preimpostato su oggi):",
            opzioni_i90,
            index=giorno_odierno_idx
        )
    with col_s2:
        doms_level = st.slider("🔥 Affaticamento muscolare (DOMS)", min_value=0, max_value=10, value=2)

    if st.button("GENERA SCHEDA CON CARICHI CALCOLATI", type="primary", use_container_width=True):
        if df_master.empty:
            st.error("Dati mancanti. Esegui prima una sincronizzazione Garmin.")
        else:
            with st.spinner("Calcolo carichi esatti e generazione protocollo I90 in corso..."):
                oggi_ts = pd.Timestamp(date.today())
                inizio_settimana = oggi_ts - pd.Timedelta(days=oggi_ts.dayofweek)
                
                ultima = df_master.iloc[-1]
                bb_attuale_energia = ultima.get('body_battery_attuale', 'N/D')
                rhr_attuale = ultima.get('rhr', 'N/D')
                peso_attuale = ultima.get('peso_kg', 75.0)
                
                if not df_attivita.empty:
                    df_attivita['data_dt'] = pd.to_datetime(df_attivita['data'])
                    storico_settimana = df_attivita[df_attivita['data_dt'] >= inizio_settimana].drop(
                        columns=['raw_data', 'data_dt'], errors='ignore'
                    ).to_dict(orient="records")
                else:
                    storico_settimana = "Nessuna attività registrata questa settimana"

                ultimi_log_pesi = df_workout_logs.tail(3).to_dict(orient="records") if not df_workout_logs.empty else "Nessun log precedente"

                prompt_i90 = f"""
                Sei il Direttore Tecnico del "Protocollo I90". Alleni Francesco (40 anni, peso {peso_attuale} kg).
                
                PARAMETRI MATEMATICI DEL CICLO ATTUALE (CALCOLATI DAL SISTEMA, NON MODIFICARLI):
                - Ciclo: {ciclo_i90} | Settimana: {settimana_i90} su 12 ({param_sett['blocco']})
                - Stato: {stato_ciclo_txt} | Percentuale carico sui fondamentali: {int(param_sett['perc_1rm']*100)}% del massimale
                - Serie e ripetizioni prescritte oggi sui fondamentali con bilanciere: {param_sett['serie_fond']} serie da {param_sett['reps_fond']} ripetizioni (margine RIR {param_sett['rir']})
                - Carichi ESATTI da prescrivere oggi:
                  * Panca piana bilanciere: {kg_panca_target} kg (su massimale di {bench_attuali['panca_1rm']} kg)
                  * Military press bilanciere: {kg_military_target} kg (su massimale di {bench_attuali['military_1rm']} kg)
                  * Squat bilanciere: {kg_squat_target} kg (su massimale di {bench_attuali['squat_1rm']} kg)
                  * Stacco da terra: {kg_stacco_target} kg (su massimale di {bench_attuali['stacco_1rm']} kg)
                  * Trazioni alla sbarra (RIGOROSAMENTE A CORPO LIBERO, zero zavorra): {param_sett['serie_cal']} serie da {reps_trazioni_target} ripetizioni controllate (su massimale di {bench_attuali['trazioni_max']} rip.)
                  * Piegamenti alle parallele - Dip (A CORPO LIBERO): {param_sett['serie_cal']} serie da {reps_dip_target} ripetizioni (su massimale di {bench_attuali['dip_max']} rip.)
                  * Curl bicipiti con bilanciere o manubri: 4 serie da 8-10 ripetizioni con {kg_curl_target} kg
                - Passo di riferimento sui 5 km per la corsa: {bench_attuali['passo_5k']} min/km.
                - Telemetria: Body Battery {bb_attuale_energia}%, FC riposo {rhr_attuale} bpm, DOMS {doms_level}/10.
                - Ultime schede pesi registrate dall'atleta: {ultimi_log_pesi}
                
                MODULO DI OGGI:
                {modulo_scelto}
                
                REGOLE DI GENERAZIONE PER IL MODULO SCELTO:
                1. Se è MARTEDÌ (Forza parte superiore + Armatura braccia):
                   - Fase 1 (Fondamentali di forza): Panca piana ({param_sett['serie_fond']}x{param_sett['reps_fond']} a {kg_panca_target} kg, rec. 120s), Trazioni alla sbarra a corpo libero ({param_sett['serie_cal']}x{reps_trazioni_target} rip. con discesa di 3 secondi, rec. 90-120s), Military press ({param_sett['serie_fond']}x{param_sett['reps_fond']} a {kg_military_target} kg, rec. 120s) e Rematore pesante con bilanciere o manubrio (4x8).
                   - Fase 2 (Blocco obbligatorio Armatura Braccia - Bicipiti e Tricipiti):
                     * Super serie 1 (Forza braccia): **Piegamenti alle parallele (Dip)** ({param_sett['serie_cal']} serie da {reps_dip_target} ripetizioni) + **Curl con bilanciere (o due manubri)** (4 serie da 8-10 ripetizioni con {kg_curl_target} kg). Recupero 75 secondi a fine super serie.
                     * Super serie 2 (Ipertrofia e pompaggio denso): **Curl a martello con manubri** (3x12) + **Estensioni tricipiti sopra la testa o ai cavi** (3x12-15) + **Alzate laterali per deltoidi** (3x15). Zero pausa tra gli esercizi, 60 secondi a fine giro.
                2. Se è GIOVEDÌ (Forza parte inferiore):
                   - Prescrivi Squat ({param_sett['serie_fond']}x{param_sett['reps_fond']} a {kg_squat_target} kg, rec. 120-150s) e Stacco ({param_sett['serie_fond']}x{param_sett['reps_fond']} a {kg_stacco_target} kg), seguiti da Affondi bulgari o pressa (4x8 per gamba), Leg curl per femorali (4x10) e 2 esercizi di Core anti-rotazionale (Plank, Ab-wheel, Pallof press).
                3. Se è SABATO (Condizionamento ibrido):
                   - Fase 1 (20 min): lavoro tecnico e di potenza con manubri o bilanciere (es. Clean & Press o Thruster) + richiamo rapido di trazioni e dip.
                   - Fase 2 (25-30 min): Circuito Metcon in stile Hyrox/CrossFit (con corsa, burpees, trazioni a corpo libero, affondi in camminata con manubri, devil press, trasporto carichi).
                4. Se è MERCOLEDÌ (Ripetute), VENERDÌ (Zona 2) o DOMENICA (Long Run):
                   - Calcola i ritmi esatti al km partendo dal suo passo sui 5 km ({bench_attuali['passo_5k']}/km). Vietato inserire esercizi di palestra.

                FORMATO OBBLIGATORIO DELL'OUTPUT:
                - 📊 **[Inquadramento tattico Ciclo {ciclo_i90} - Settimana {settimana_i90}]**
                - ⚙️ **[Protocollo operativo con carichi esatti]**
                - 🧠 **[Standard mentale I90: fatto o non fatto]**
                """
                try:
                    risposta = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "system", "content": prompt_i90}],
                        temperature=0.5
                    )
                    st.session_state["ultima_scheda_generata"] = risposta.choices[0].message.content
                except Exception as e:
                    st.error(f"Errore API OpenAI: {e}")

    if "ultima_scheda_generata" in st.session_state:
        st.markdown(st.session_state["ultima_scheda_generata"])

    st.markdown("---")
    st.subheader("📝 Registra i carichi sollevati oggi (Diario dell'atleta)")
    with st.form("form_log_allenamento"):
        col_l1, col_l2, col_l3, col_l4 = st.columns(4)
        with col_l1:
            log_panca_kg = st.number_input("Panca piana (kg)", value=float(kg_panca_target), step=2.5)
            log_military_kg = st.number_input("Military press (kg)", value=float(kg_military_target), step=2.5)
        with col_l2:
            log_trazioni_reps = st.number_input("Trazioni totali chiuse", value=int(param_sett['serie_cal'] * reps_trazioni_target), step=1)
            log_dip_reps = st.number_input("Dip parallele totali chiusi", value=int(param_sett['serie_cal'] * reps_dip_target), step=1)
        with col_l3:
            log_curl_kg = st.number_input("Curl bicipiti (kg)", value=float(kg_curl_target), step=1.0)
            log_squat_kg = st.number_input("Squat (kg)", value=float(kg_squat_target), step=2.5)
        with col_l4:
            log_stacco_kg = st.number_input("Stacco da terra (kg)", value=float(kg_stacco_target), step=2.5)
            note_sessione = st.text_input("Sensazioni o tempi circuito:")
        
        if st.form_submit_button("SALVA REGISTRO SEDUTA SU SUPABASE", type="primary", use_container_width=True):
            payload_log = {
                "ciclo": ciclo_i90,
                "settimana": settimana_i90,
                "modulo": modulo_scelto.split(":")[1].strip() if ":" in modulo_scelto else modulo_scelto,
                "panca_kg": log_panca_kg,
                "trazioni_reps_totali": log_trazioni_reps,
                "dip_reps_totali": log_dip_reps,
                "curl_kg": log_curl_kg,
                "military_kg": log_military_kg,
                "squat_kg": log_squat_kg,
                "stacco_kg": log_stacco_kg,
                "note": note_sessione
            }
            try:
                salva_dato_i90_giornaliero("i90_workout_log", payload_log)
                st.cache_data.clear()
                st.success(f"✅ Seduta registrata nel Ciclo {ciclo_i90} (Settimana {settimana_i90})!")
                st.rerun()
            except Exception as e:
                st.error(f"Errore salvataggio log: {e}")

# ==========================================
# SCHEDA 3: MASSIMALI, CALCOLATORE DI SICUREZZA E CICLI
# ==========================================
with tab_carichi:
    st.header("📈 Calcolatore indiretto in sicurezza e gestione massimali")
    
    # --- NUOVO CALCOLATORE SUBMASSIMALE DI BRZYCKI ---
    with st.expander("🛡️ NON CONOSCI I TUOI MASSIMALI? Calcolali qui senza rischiare sotto i pesi (Metodo di Brzycki)", expanded=True):
        st.markdown(
            "**Come eseguire il test in palestra senza rischi:** carica un peso che riesci a muovere con tecnica perfetta per **5-8 ripetizioni**, "
            "fermandoti quando senti di avere ancora 1 ripetizione di margine. Inserisci qui sotto il peso usato e le ripetizioni totali (incluse quelle di margine): "
            "il sistema calcolerà il tuo massimale teorico su 1 ripetizione e lo copierà automaticamente nella scheda sotto."
        )
        cc1, cc2, cc3, cc4 = st.columns(4)
        with cc1:
            st.markdown("**1. Panca piana**")
            t_panca_kg = st.number_input("Peso usato su panca (kg)", value=65.0, step=2.5, key="tp_kg")
            t_panca_r = st.number_input("Ripetizioni valide (panca)", min_value=2, max_value=12, value=6, step=1, key="tp_r")
            stima_panca = round((t_panca_kg / (1.0278 - 0.0278 * t_panca_r)) / 2.5) * 2.5
            st.caption(f"Massimale stimato: **{stima_panca} kg**")
        with cc2:
            st.markdown("**2. Squat bilanciere**")
            t_squat_kg = st.number_input("Peso usato su squat (kg)", value=80.0, step=2.5, key="tsq_kg")
            t_squat_r = st.number_input("Ripetizioni valide (squat)", min_value=2, max_value=12, value=6, step=1, key="tsq_r")
            stima_squat = round((t_squat_kg / (1.0278 - 0.0278 * t_squat_r)) / 2.5) * 2.5
            st.caption(f"Massimale stimato: **{stima_squat} kg**")
        with cc3:
            st.markdown("**3. Stacco da terra**")
            t_stacco_kg = st.number_input("Peso usato su stacco (kg)", value=95.0, step=2.5, key="tst_kg")
            t_stacco_r = st.number_input("Ripetizioni valide (stacco)", min_value=2, max_value=12, value=6, step=1, key="tst_r")
            stima_stacco = round((t_stacco_kg / (1.0278 - 0.0278 * t_stacco_r)) / 2.5) * 2.5
            st.caption(f"Massimale stimato: **{stima_stacco} kg**")
        with cc4:
            st.markdown("**4. Military press**")
            t_mil_kg = st.number_input("Peso usato su military (kg)", value=40.0, step=2.5, key="tm_kg")
            t_mil_r = st.number_input("Ripetizioni valide (military)", min_value=2, max_value=12, value=6, step=1, key="tm_r")
            stima_mil = round((t_mil_kg / (1.0278 - 0.0278 * t_mil_r)) / 2.5) * 2.5
            st.caption(f"Massimale stimato: **{stima_mil} kg**")

        if st.button("⬇️ COPIA QUESTE STIME NEI CAMPI UFFICIALI QUI SOTTO", use_container_width=True):
            st.session_state["input_panca_1rm"] = float(stima_panca)
            st.session_state["input_squat_1rm"] = float(stima_squat)
            st.session_state["input_stacco_1rm"] = float(stima_stacco)
            st.session_state["input_military_1rm"] = float(stima_mil)
            st.success("Valori copiati! Premi 'AGGIORNA MASSIMALI E SALVA NEL CICLO' qui sotto per renderli attivi.")

    with st.form("form_benchmarks"):
        st.subheader(f"Parametri ufficiali di riferimento - Ciclo {ciclo_i90}, Settimana {settimana_i90}")
        cb1, cb2, cb3, cb4 = st.columns(4)
        with cb1:
            b_panca = st.number_input("Panca piana - Massimale (kg)", value=float(st.session_state["input_panca_1rm"]), step=2.5)
            b_military = st.number_input("Military press - Massimale (kg)", value=float(st.session_state["input_military_1rm"]), step=2.5)
        with cb2:
            b_squat = st.number_input("Squat - Massimale (kg)", value=float(st.session_state["input_squat_1rm"]), step=2.5)
            b_stacco = st.number_input("Stacco - Massimale (kg)", value=float(st.session_state["input_stacco_1rm"]), step=2.5)
        with cb3:
            b_trazioni = st.number_input("Trazioni libere - Ripetizioni max", value=int(st.session_state["input_trazioni_max"]), step=1)
            b_dip = st.number_input("Dip parallele liberi - Ripetizioni max", value=int(st.session_state["input_dip_max"]), step=1)
        with cb4:
            b_curl = st.number_input("Curl bicipiti - Carico per 8-10 rip. (kg)", value=float(st.session_state["input_curl_kg"]), step=1.0)
            b_passo5k = st.text_input("Corsa 5 km - Passo medio (MM:SS)", value=str(st.session_state["input_passo_5k"]))
            
        if st.form_submit_button("AGGIORNA MASSIMALI E SALVA NEL CICLO", type="primary", use_container_width=True):
            payload_b = {
                "ciclo": ciclo_i90,
                "settimana": settimana_i90,
                "panca_1rm": b_panca,
                "military_1rm": b_military,
                "squat_1rm": b_squat,
                "stacco_1rm": b_stacco,
                "trazioni_max": b_trazioni,
                "dip_max": b_dip,
                "curl_kg": b_curl,
                "passo_5k": b_passo5k
            }
            try:
                salva_dato_i90_giornaliero("i90_benchmarks", payload_b)
                st.cache_data.clear()
                st.success("✅ Massimali salvati! Tutti i carichi nella barra laterale sono stati ricalibrati.")
                st.rerun()
            except Exception as e:
                st.error(f"Errore salvataggio massimali: {e}")

    st.markdown("---")
    st.subheader("🏆 Confronto progressi tra cicli (Stagioni di 12 settimane)")
    if not df_benchmarks.empty:
        df_b_view = df_benchmarks.copy()
        df_b_view["data"] = pd.to_datetime(df_b_view["data"]).dt.strftime("%d/%m/%Y")
        df_b_view["Etichetta"] = "Ciclo " + df_b_view["ciclo"].astype(str) + " (Sett. " + df_b_view["settimana"].astype(str) + ")"
        
        col_g1, col_g2 = st.columns(2)
        with col_g1:
            fig_forza = px.bar(
                df_b_view, x="Etichetta", y=["panca_1rm", "squat_1rm", "stacco_1rm", "military_1rm"],
                barmode="group", title="Evoluzione massimali con bilanciere (kg)",
                labels={"value": "Chilogrammi (kg)", "variable": "Fondamentale"}
            )
            st.plotly_chart(fig_forza, use_container_width=True)
        with col_g2:
            cols_cal = [c for c in ["trazioni_max", "dip_max"] if c in df_b_view.columns]
            fig_cal = px.line(
                df_b_view, x="Etichetta", y=cols_cal, markers=True,
                title="Evoluzione trazioni e dip a corpo libero",
                labels={"value": "Ripetizioni massime", "variable": "Esercizio"}
            )
            st.plotly_chart(fig_cal, use_container_width=True)
            
        st.dataframe(df_b_view, use_container_width=True)
    else:
        st.info("Usa il calcolatore qui sopra e salva i tuoi parametri per iniziare a tracciare l'evoluzione.")

# ==========================================
# SCHEDA 4: LO STILE DI VITA (LE 4 REGOLE ON/OFF)
# ==========================================
with tab_mindset:
    st.header("🧠 Le 4 regole quotidiane - Legge binaria: fatto o non fatto")
    st.write("Ogni regola è un interruttore: **ON** o **OFF**. Niente 'fatto a metà'.")
    
    oggi_str = date.today().isoformat()
    stato_iniziale = {"regola_1": False, "regola_2": False, "regola_3": False, "regola_4": False, "vittorie": "", "piano_domani": ""}
    
    if not df_master.empty:
        riga_oggi = df_master[df_master['data'] == pd.Timestamp(oggi_str)]
        if not riga_oggi.empty:
            raw_temp = riga_oggi.iloc[-1].get('raw_data', {})
            if isinstance(raw_temp, dict) and "i90_mindset" in raw_temp:
                stato_iniziale.update(raw_temp["i90_mindset"])

    with st.form("form_mindset_i90"):
        st.subheader(f"Registro di disciplina del {date.today().strftime('%d/%m/%Y')}")
        c1, c2 = st.columns(2)
        with c1:
            r1 = st.checkbox("1. 🥩 Alimentazione pulita (zero alcol, zero cibo spazzatura, zero calorie liquide)", value=stato_iniziale["regola_1"])
            r2 = st.checkbox("2. 📖 Allenamento mentale (10 pagine di saggistica: crescita, psicologia, impresa)", value=stato_iniziale["regola_2"])
        with c2:
            r3 = st.checkbox("3. 🎯 Programma il domani (rituale Navy SEAL: 3 vittorie + piano di domani)", value=stato_iniziale["regola_3"])
            r4 = st.checkbox("4. ❄️ Rituale di scomodità (doccia fredda di almeno 1 minuto ogni mattina)", value=stato_iniziale["regola_4"])
            
        st.markdown("---")
        col_t1, col_t2 = st.columns(2)
        with col_t1:
            vittorie_txt = st.text_area("🏆 Le 3 vittorie di oggi:", value=stato_iniziale["vittorie"], height=100)
        with col_t2:
            piano_txt = st.text_area("🗺️ Pianificazione chirurgica di domani:", value=stato_iniziale["piano_domani"], height=100)
            
        if st.form_submit_button("REGISTRA GIORNATA SUL DATABASE", type="primary", use_container_width=True):
            fatto_totale = bool(r1 and r2 and r3 and r4)
            payload_m = {
                "ciclo": ciclo_i90,
                "settimana": settimana_i90,
                "regola_1": r1, "regola_2": r2, "regola_3": r3, "regola_4": r4,
                "vittorie": vittorie_txt, "piano_domani": piano_txt,
                "esito_binario": "FATTO" if fatto_totale else "NON FATTO"
            }
            try:
                salva_dato_i90_giornaliero("i90_mindset", payload_m)
                st.cache_data.clear()
                if fatto_totale:
                    st.success("🟢 STATO GIORNALIERO: FATTO (4/4 ON). Disciplina confermata.")
                else:
                    st.error("🔴 STATO GIORNALIERO: NON FATTO (OFF). Completa i pilastri mancanti.")
            except Exception as e:
                st.error(f"Errore durante il salvataggio: {e}")

# ==========================================
# SCHEDA 5: COMPOSIZIONE CORPOREA (ZEPP)
# ==========================================
with tab_inserimento:
    st.header("⚖️ Inserimento parametri strutturali (Zepp)")
    st.write(f"Data misurazione: **{date.today().strftime('%d/%m/%Y')}**")

    with st.form("zepp_form"):
        col1, col2, col3 = st.columns(3)
        with col1:
            peso = st.number_input("Peso (kg)", value=75.0, step=0.1)
            massa_grassa = st.number_input("Massa grassa %", value=15.0, step=0.1)
            grasso_viscerale = st.number_input("Grasso viscerale", value=5.0, step=0.1)
            imc = st.number_input("IMC", value=22.0, step=0.1)
        with col2:
            muscoli = st.number_input("Muscoli (kg)", value=55.0, step=0.1)
            muscolatura_scheletrica = st.number_input("Muscolatura scheletrica (kg)", value=30.0, step=0.1)
            acqua = st.number_input("Acqua %", value=60.0, step=0.1)
        with col3:
            proteine = st.number_input("Proteine %", value=18.0, step=0.1)
            grasso_sottocutaneo = st.number_input("Grasso sottocutaneo %", value=10.0, step=0.1)
            massa_ossea = st.number_input("Massa ossea (kg)", value=3.0, step=0.1)
            bmr = st.number_input("Metabolismo basale (kcal)", value=1800, step=1)
            
        if st.form_submit_button("SALVA DATI SU DATABASE", type="primary", use_container_width=True):
            payload_zepp = {
                "data": date.today().isoformat(), "peso_kg": peso, "acqua_perc": acqua,
                "grasso_sottocutaneo_perc": grasso_sottocutaneo, "grasso_viscerale": grasso_viscerale,
                "imc": imc, "massa_grassa_perc": massa_grassa, "muscoli_kg": muscoli,
                "proteine_perc": proteine, "bmr_kcal": bmr, "massa_ossea_kg": massa_ossea,
                "muscolatura_scheletrica_kg": muscolatura_scheletrica
            }
            try:
                requests.post(
                    f"{supabase_url}/rest/v1/composizione_corporea",
                    headers={
                        "apikey": supabase_key,
                        "Authorization": f"Bearer {supabase_key}",
                        "Content-Type": "application/json",
                        "Prefer": "resolution=merge-duplicates"
                    },
                    json=payload_zepp
                ).raise_for_status()
                st.success("✅ Dati corporei salvati.")
                st.cache_data.clear()
            except Exception as e:
                st.error(e)

# ==========================================
# FIRMA
# ==========================================
st.markdown("---")
st.markdown("<p style='text-align: center; color: gray; font-weight: bold; letter-spacing: 2px;'>ENGINEERED BY FRANCESCO | PROTOCOLLO I90 OPERATING SYSTEM</p>", unsafe_allow_html=True)
