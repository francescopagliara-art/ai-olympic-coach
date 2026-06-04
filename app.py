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

st.set_page_config(page_title="AI Olympic Coach | The Notorious", page_icon="🐺", layout="wide")

# ==========================================
# 2. MOTORE DI SINCRONIZZAZIONE (GARMIN -> SUPABASE)
# ==========================================
def sincronizza_garmin_completo():
    try:
        garmin = Garmin(garmin_email, garmin_password)
        garmin.login()
        oggi = date.today().isoformat()
        
        headers_upsert = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}", "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}

        # --- A. Estrazione Metriche Giornaliere ---
        stats = garmin.get_stats(oggi)
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
            "data": oggi, "sonno_ore": sonno_ore, "sonno_punteggio": sonno_punteggio,
            "hrv_status": stats.get('hrvStatus'), "rhr": stats.get('restingHeartRate'),
            "body_battery_max": stats.get('bodyBatteryHighestValue'), "body_battery_min": stats.get('bodyBatteryLowestValue'),
            "body_battery_attuale": bb_attuale,
            "stress_medio": stats.get('averageStressLevel'), "passi": stats.get('steps'), "raw_data": stats
        }
        requests.post(f"{supabase_url}/rest/v1/metrica_giornaliera", headers=headers_upsert, json=payload_giornaliero).raise_for_status()

        # --- B. Estrazione Ultime Attività Sportive ---
        headers_insert = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}", "Content-Type": "application/json"}
        activities = garmin.get_activities(0, 5) 
        for act in activities:
            act_date = act.get('startTimeLocal', '').split(' ')[0]
            if not act_date: continue
            
            payload_act = {
                "data": act_date, "attivita_id_garmin": str(act.get('activityId')),
                "tipo_sport": str(act.get('activityType', {}).get('typeKey', 'sconosciuto')), "raw_data": act
            }
            
            if act.get('duration'): payload_act["durata_minuti"] = round(float(act['duration']) / 60, 2)
            if act.get('averageHR'): payload_act["fc_media"] = int(act['averageHR'])
            if act.get('maxHR'): payload_act["fc_max"] = int(act['maxHR'])
            if act.get('aerobicTrainingEffect'): payload_act["training_effect_aerobico"] = float(act['aerobicTrainingEffect'])
            if act.get('anaerobicTrainingEffect'): payload_act["training_effect_anaerobico"] = float(act['anaerobicTrainingEffect'])
            
            t_load = act.get('trainingLoad') or act.get('vO2MaxValue')
            if t_load: payload_act["training_load"] = float(t_load)
            
            try:
                res = requests.post(f"{supabase_url}/rest/v1/attivita_sportive", headers=headers_insert, json=payload_act)
                res.raise_for_status()
            except requests.exceptions.HTTPError as e:
                if res.status_code in [409, 400]:
                    continue
        return True
    except Exception as e:
        st.error(f"Errore critico Garmin: {e}")
        return False

# ==========================================
# 3. INTERFACCIA: SIDEBAR
# ==========================================
with st.sidebar:
    st.header("⚡ TERMINALE")
    if st.button("🔄 SINCRONIZZA GARMIN", type="primary", use_container_width=True):
        with st.spinner("Estrazione telemetria real-time in corso..."):
            if sincronizza_garmin_completo():
                st.success("Sincronizzazione completata! Parametri aggiornati.")
                st.cache_data.clear()

# ==========================================
# 4. MOTORE DI LETTURA DATI (ETL DA SUPABASE)
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
        
        # FIX: FORWARD FILL. Riempie i giorni vuoti di Zepp con l'ultima misurazione valida.
        df_completo = df_completo.ffill()
        
        df_completo['massa_grassa_kg'] = df_completo['peso_kg'] * (df_completo['massa_grassa_perc'] / 100)
        df_completo['acqua_kg'] = df_completo['peso_kg'] * (df_completo['acqua_perc'] / 100)
        
    return df_completo, df_attivita

df_master, df_attivita = fetch_data()

# ==========================================
# 5. TITOLO E SCHEDE
# ==========================================
st.title("🐺 The Notorious Protocol - AI Coach")
st.markdown("---")

tab_dashboard, tab_coach, tab_inserimento = st.tabs(["📊 TELEMETRIA", "🧠 AI COACH", "⚖️ DATA ENTRY"])

# ==========================================
# TAB 1: DASHBOARD (GOD MODE NATIVA)
# ==========================================
with tab_dashboard:
    if df_master.empty:
        st.warning("Nessun dato disponibile nel database. Sincronizza Garmin o inserisci dati Zepp.")
    else:
        ultima_riga = df_master.iloc[-1]
        raw = ultima_riga.get('raw_data', {})
        if isinstance(raw, str): raw = {} 
        
        # --- METRICHE VITALI (Incluso VO2 Max) ---
        col1, col2, col3, col4 = st.columns(4)
        
        # 1. VO2 Max
        vo2 = raw.get('vo2MaxValue', 'N/D')
        if vo2 == 'N/D':
            for _, act in df_attivita.head(5).iterrows():
                act_raw = act.get('raw_data', {})
                if isinstance(act_raw, dict) and act_raw.get('vO2MaxValue'):
                    vo2 = act_raw.get('vO2MaxValue')
                    break
        col1.metric("VO2 Max (Motore)", f"{vo2}")
        
        # 2. FC Riposo
        fc = ultima_riga.get('rhr', 'N/D')
        col2.metric("FC Riposo", f"{fc} bpm")
        
        # 3. Calorie Bruciate
        calorie = raw.get('totalKilocalories')
        col3.metric("Calorie Totali", f"{int(calorie)} kcal" if calorie else "N/D")
        
        # 4. Stress
        stress = ultima_riga.get('stress_medio', 'N/D')
        col4.metric("Stress Medio", f"{stress} / 100")
        
        st.markdown("---")

        # --- ULTIMA ATTIVITÀ SPORTIVA ---
        st.subheader("🏅 Ultimo Ingaggio sul Campo")
        if not df_attivita.empty:
            ultima_att = df_attivita.iloc[0]
            col_a1, col_a2, col_a3, col_a4 = st.columns(4)
            
            tipo = str(ultima_att.get('tipo_sport', 'N/D')).replace('_', ' ').title()
            col_a1.metric("Disciplina", tipo)
            
            durata = ultima_att.get('durata_minuti')
            col_a2.metric("Durata", f"{durata} min" if not pd.isna(durata) else "N/D")
            
            fc_m = ultima_att.get('fc_media')
            col_a3.metric("FC Media", f"{int(fc_m)} bpm" if not pd.isna(fc_m) else "N/D")
            
            t_load = ultima_att.get('training_load')
            col_a4.metric("Training Load", str(int(t_load)) if not pd.isna(t_load) else "N/D")
        else:
            st.info("Nessuna attività registrata.")
        st.markdown("---")

        # --- GRAFICI INTERATTIVI (NATIVI STREAMLIT) ---
        col_g1, col_g2 = st.columns(2)
        
        with col_g1:
            bb_attuale_val = ultima_riga.get('body_battery_attuale')
            if pd.isna(bb_attuale_val): bb_attuale_val = ultima_riga.get('body_battery_max', 0) 

            fig_gauge = go.Figure(go.Indicator(
                mode = "gauge+number", value = bb_attuale_val,
                title = {'text': "SERBATOIO ENERGETICO (Body Battery)"},
                gauge = {
                    'axis': {'range': [0, 100]}, 
                    'bar': {'color': "#3b82f6"}, # Azzurro Elettrico per massimo contrasto
                    'steps': [{'range': [0, 30], 'color': "#ff4b4b"}, {'range': [30, 70], 'color': "#ffa600"}, {'range': [70, 100], 'color': "#00cc96"}]
                }
            ))
            st.plotly_chart(fig_gauge, use_container_width=True)

            fig_scatter = px.scatter(
                df_master, x="sonno_ore", y="rhr", size="body_battery_max", color="stress_medio",
                title="EFFICIENZA RECUPERO (Sonno vs RHR)",
                labels={"sonno_ore": "Ore di Sonno", "rhr": "Battiti a Riposo (RHR)"}, size_max=20,
                color_continuous_scale=px.colors.sequential.Reds
            )
            st.plotly_chart(fig_scatter, use_container_width=True)

        with col_g2:
            comp_data = pd.DataFrame({
                'Componente': ['Massa Muscolare', 'Massa Grassa', 'Massa Ossea', 'Grasso Viscerale (Indice)', 'Acqua Corporea'],
                'Valori': [ultima_riga.get('muscoli_kg', 0), ultima_riga.get('massa_grassa_kg', 0), ultima_riga.get('massa_ossea_kg', 0), ultima_riga.get('grasso_viscerale', 0), ultima_riga.get('acqua_kg', 0)]
            })
            comp_data = comp_data[comp_data['Valori'] > 0]
            fig_tree = px.treemap(
                comp_data, path=['Componente'], values='Valori', 
                title="STRUTTURA CORPOREA (kg)", 
                color='Valori', color_continuous_scale='Reds'
            )
            st.plotly_chart(fig_tree, use_container_width=True)

# ==========================================
# TAB 2: IL COACH AI (OPENAI GPT-4o-mini)
# ==========================================
with tab_coach:
    st.header("🧠 GENERAZIONE PROTOCOLLO")
    st.write("Inserisci i parametri di ingaggio. Il Coach calibrerà lo stress sul tuo reale stato fisiologico e muscolare.")
    
    col_scelta1, col_scelta2 = st.columns(2)
    with col_scelta1:
        sport_scelto = st.selectbox("🎯 Disciplina Odierna:", ["Seleziona...", "🏃 Corsa", "🏋️ Sala Pesi", "🚴 Ciclismo"])
    
    with col_scelta2:
        doms_level = st.slider(
            "🔥 Livello DOMS / Affaticamento Muscolare", 
            min_value=0, max_value=10, value=0, 
            help="0 = Fresco e reattivo. 10 = Dolori estremi, fatico a muovermi."
        )

    st.markdown("---")
    
    if sport_scelto != "Seleziona...":
        if st.button(f"GENERA PROTOCOLLO: {sport_scelto}", type="primary", use_container_width=True):
            if df_master.empty:
                st.error("Dati mancanti. Esegui prima una sincronizzazione Garmin.")
            else:
                with st.spinner(f'Elaborazione balistica in corso per {sport_scelto}...'):
                    
                    oggi = pd.Timestamp(date.today())
                    inizio_settimana = oggi - pd.Timedelta(days=oggi.dayofweek)
                    
                    storico_settimana = df_master[df_master['data'] >= inizio_settimana].to_dict(orient="records")
                    bb_attuale_energia = df_master.iloc[-1].get('body_battery_attuale', 'N/D')
                    
                    # Estrazione dinamica e automatica dei dati corporei per la Nutrizione
                    peso_attuale = df_master.iloc[-1].get('peso_kg', 75.0)
                    muscoli_attuali = df_master.iloc[-1].get('muscoli_kg', 55.0)
                    
                    if not df_attivita.empty:
                        df_attivita['data'] = pd.to_datetime(df_attivita['data'])
                        storico_attivita = df_attivita[df_attivita['data'] >= inizio_settimana].drop(columns=['raw_data'], errors='ignore').to_dict(orient="records")
                    else:
                        storico_attivita = "Nessuna attività registrata in questa settimana."
                    
                    prompt_di_sistema = f"""
                    Sei "The Notorious", il Coach Olimpionico e Stratega della Nutrizione Sportiva più estremo e analitico al mondo. 
                    Non alleni dilettanti. Alleni macchine. Zero pietà, esecuzione ingegneristica.

                    PROFILO ATLETA:
                    - Nome: Francesco. 40 anni, ingegnere manageriale, mente ferrea.
                    - Struttura Corporea: {peso_attuale} kg. Massa muscolare pura: {muscoli_attuali} kg.
                    - Motore Aerobico ELITE: 55-65 km/settimana di corsa. Resistenza inesauribile, battiti a riposo da rettile.
                    - Obiettivo Pesi: Muscolo denso, funzionale, forza balistica pura (transfer totale sulla corsa). Zero estetica, 100% performance.

                    Disciplina odierna: {sport_scelto}.

                    REGOLE D'INGAGGIO TASSATIVE:
                    1. FATICA E TELEMETRIA: Body Battery Istantanea: {bb_attuale_energia}%.
                    2. FEEDBACK UMANO: Livello di DOMS: {doms_level}/10. 
                       - Se DOMS <= 3: Distruggilo. Massimizza il carico nervoso e meccanico.
                       - Se DOMS tra 4 e 7: Modula chirurgicamente. Aggira i tessuti infiammati, riduci il volume.
                       - Se DOMS >= 8: Imponi recupero attivo o mobilità. Niente carichi massimali.

                    3. PROTOCOLLO ESECUTIVO (SALA PESI):
                       - RISCALDAMENTO OBBLIGATORIO: 5-7 minuti ESATTI. Includi lavoro specifico su core/addome (Plank, Hollow, ecc.) e mobilità articolare.
                       - LAVORO CENTRALE (35-40 min): Altissima densità. Usa protocolli EMOM, AMRAP o Giant Sets. Fornisci il dettaglio chirurgico: Nomi esercizi, Serie, Reps, Recupero al secondo. REGOLA BIOMECCANICA UNIVERSALE (STILE CROSSFIT ELITE): Massimizza l'impatto metabolico ma distribuisci il danno. Alterna rigorosamente i pattern motori nei circuiti. Il sovraccarico deve essere sistemico: evita di affaticare la stessa articolazione in due stazioni consecutive.
                       - VARIABILITÀ E ADATTAMENTO NEURONALE: Applica il principio della rotazione coniugata. Cambia sempre la selezione degli esercizi e degli angoli di lavoro rispetto alle sessioni precedenti. Vietato generare lo stesso workout per due giorni consecutivi.
                       - DEFATICAMENTO: Assente. Finito il circuito, esci dalla palestra.

                    4. PROTOCOLLO ESECUTIVO (CORSA/CICLISMO):
                       - CORSA: Volumi estremi. Ripetute, fartlek o tempo run con passi al secondo esatti.
                       - CICLISMO: Base di Watt (%FTP) e Cadenza (RPM).

                    5. NUTRIZIONE PREDITTIVA (BIO-HACKING DEL RECUPERO):
                       - Calcola il dispendio calorico preciso della sessione.
                       - PRE-WORKOUT: Grammi esatti di carbo e timing per saturare il glicogeno.
                       - POST-WORKOUT: Grammi esatti di proteine isolate e carbo ad alto IG per spegnere il cortisolo e innescare la sintesi proteica.

                    FORMAT DI RISPOSTA OBBLIGATORIO (Usa il Markdown in modo aggressivo, elenchi puntati rigorosi):
                    - 📊 **[TELEMETRIA INGAGGIATA]**: Analisi di Body Battery e DOMS ({doms_level}/10).
                    - 🧠 **[STRATEGIA NOTORIOUS]**: La logica bio-meccanica di oggi.
                    - ⚙️ **[PROTOCOLLO OPERATIVO]**: 
                      * **Fase 1: Riscaldamento e Core (5-7 min)**: Dettaglio esercizi e tempi.
                      * **Fase 2: Lavoro Centrale (35-40 min)**: Dettaglio estremo del circuito (Serie, Reps, Riposi).
                    - 🥩 **[BIO-HACKING NUTRIZIONALE]**: I macro-nutrienti esatti.
                    """
                    
                    prompt_utente = f"Microciclo: {storico_settimana}. Attività: {storico_attivita}. Body Battery: {bb_attuale_energia}%. DOMS: {doms_level}/10. Disciplina: {sport_scelto}."
                    
                    try:
                        risposta = client.chat.completions.create(
                            model="gpt-4o-mini",
                            messages=[{"role": "system", "content": prompt_di_sistema}, {"role": "user", "content": prompt_utente}],
                            temperature=0.7 # Temperatura alzata per massimizzare la creatività tecnica e sbloccare la varianza degli esercizi
                        )
                        st.success("PROTOCOLLO AGGANCIATO.")
                        st.markdown(f"### 🐺 Protocollo {sport_scelto}")
                        st.write(risposta.choices[0].message.content)
                    except Exception as e:
                        st.error(f"Errore API OpenAI: {e}")

# ==========================================
# TAB 3: INSERIMENTO MANUALE DATI ZEPP
# ==========================================
with tab_inserimento:
    st.header("⚖️ INSERIMENTO DATI (ZEPP)")
    st.write(f"Data misurazione: **{date.today().strftime('%d/%m/%Y')}**")

    with st.form("zepp_form"):
        col1, col2, col3 = st.columns(3)
        with col1:
            peso = st.number_input("Peso (kg)", value=75.0, step=0.1)
            massa_grassa = st.number_input("Massa Grassa %", value=15.0, step=0.1)
            grasso_viscerale = st.number_input("Grasso Viscerale", value=5.0, step=0.1)
            imc = st.number_input("IMC", value=22.0, step=0.1)
        with col2:
            muscoli = st.number_input("Muscoli (kg)", value=55.0, step=0.1)
            muscolatura_scheletrica = st.number_input("Muscolatura Scheletrica (kg)", value=30.0, step=0.1)
            acqua = st.number_input("Acqua %", value=60.0, step=0.1)
        with col3:
            proteine = st.number_input("Proteine %", value=18.0, step=0.1)
            grasso_sottocutaneo = st.number_input("Grasso Sottocutaneo %", value=10.0, step=0.1)
            massa_ossea = st.number_input("Massa Ossea (kg)", value=3.0, step=0.1)
            bmr = st.number_input("Metabolismo Basale (kcal)", value=1800, step=1)
            
        if st.form_submit_button("SALVA DATI SU DATABASE", type="primary"):
            payload_zepp = {
                "data": date.today().isoformat(), "peso_kg": peso, "acqua_perc": acqua,
                "grasso_sottocutaneo_perc": grasso_sottocutaneo, "grasso_viscerale": grasso_viscerale,
                "imc": imc, "massa_grassa_perc": massa_grassa, "muscoli_kg": muscoli,
                "proteine_perc": proteine, "bmr_kcal": bmr, "massa_ossea_kg": massa_ossea,
                "muscolatura_scheletrica_kg": muscolatura_scheletrica
            }
            try:
                requests.post(f"{supabase_url}/rest/v1/composizione_corporea", headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}", "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}, json=payload_zepp).raise_for_status()
                st.success("✅ Dati corporei salvati. Sincronizzazione strutturale completata.")
            except Exception as e:
                st.error(e)

# ==========================================
# FOOTER / FIRMA
# ==========================================
st.markdown("---")
st.markdown("<p style='text-align: center; color: gray; font-weight: bold; letter-spacing: 2px;'>ENGINEERED BY FRANCESCO | THE NOTORIOUS PROTOCOL</p>", unsafe_allow_html=True)
