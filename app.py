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
        headers_insert = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}", "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}
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
                pass 
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
        
        df_completo = df_completo.ffill()
        
        df_completo['massa_grassa_kg'] = df_completo['peso_kg'] * (df_completo['massa_grassa_perc'] / 100)
        df_completo['acqua_kg'] = df_completo['peso_kg'] * (df_completo['acqua_perc'] / 100)
        
    return df_completo, df_attivita

df_master, df_attivita = fetch_data()

# ==========================================
# 5. TITOLO E SCHEDE
# ==========================================
st.title("🐺 The Notorious Protocol - AI Coach (Olympic Mode)")
st.markdown("---")

tab_dashboard, tab_coach, tab_inserimento = st.tabs(["📊 TELEMETRIA", "🧠 AI COACH", "⚖️ DATA ENTRY"])

# ==========================================
# TAB 1: DASHBOARD
# ==========================================
with tab_dashboard:
    if df_master.empty:
        st.warning("Nessun dato disponibile nel database. Sincronizza Garmin o inserisci dati Zepp.")
    else:
        ultima_riga = df_master.iloc[-1]
        raw = ultima_riga.get('raw_data', {})
        if isinstance(raw, str): raw = {} 
        
        col1, col2, col3, col4 = st.columns(4)
        
        sonno = ultima_riga.get('sonno_ore')
        sonno_val = round(sonno, 1) if pd.notna(sonno) else "N/D"
        col1.metric("Ore di Sonno", f"{sonno_val} h", help="Ore totali di sonno registrate l'ultima notte. Fondamentale per il calcolo del recupero e la Body Battery.")
        
        fc = ultima_riga.get('rhr')
        fc_val = int(fc) if pd.notna(fc) else "N/D"
        col2.metric("FC Riposo", f"{fc_val} bpm", help="Resting Heart Rate (RHR). I battiti medi a riposo. Più sono bassi, più il tuo motore aerobico è efficiente e il cuore pompa sangue senza sforzo.")
        
        calorie = raw.get('totalKilocalories')
        cal_val = int(calorie) if pd.notna(calorie) else "N/D"
        col3.metric("Calorie Totali", f"{cal_val} kcal", help="Stima delle calorie totali (basali + attive) bruciate finora nella giornata odierna.")
        
        stress = ultima_riga.get('stress_medio')
        stress_val = int(stress) if pd.notna(stress) else "N/D"
        col4.metric("Stress Medio", f"{stress_val} / 100", help="Heart Rate Variability (HRV). Misura la tensione del sistema nervoso. 0-25: Riposo. 26-50: Basso. 51-75: Medio. 76-100: Alto (Sovrallenamento).")
        st.markdown("---")

        st.subheader("🏅 Ultimo Ingaggio sul Campo")
        if not df_attivita.empty:
            ultima_att = df_attivita.iloc[0]
            col_a1, col_a2, col_a3, col_a4 = st.columns(4)
            
            tipo = str(ultima_att.get('tipo_sport', 'N/D')).replace('_', ' ').title()
            col_a1.metric("Disciplina", tipo, help="L'ultima attività sportiva registrata.")
            
            durata = ultima_att.get('durata_minuti')
            col_a2.metric("Durata", f"{durata} min" if pd.notna(durata) else "N/D", help="Tempo totale sotto tensione dell'ultimo allenamento.")
            
            fc_m = ultima_att.get('fc_media')
            col_a3.metric("FC Media", f"{int(fc_m)} bpm" if pd.notna(fc_m) else "N/D", help="Frequenza cardiaca media sostenuta durante tutta la sessione.")
            
            te_aerobico = ultima_att.get('training_effect_aerobico')
            col_a4.metric("Impatto Aerobico", f"{te_aerobico} TE" if pd.notna(te_aerobico) else "N/D", help="Training Effect (TE). Misura l'impatto sulla tua resistenza aerobica. 1.0-1.9: Recupero. 2.0-2.9: Mantenimento. 3.0-3.9: Miglioramento. 4.0-4.9: Netto miglioramento. 5.0: Sovraccarico (Overreaching).")
        else:
            st.info("Nessuna attività registrata.")
            
        # ==========================================
        # NUOVO MODULO: DEBRIEFING DELL'AI COACH
        # ==========================================
        if not df_attivita.empty:
            with st.expander("🎙️ RICHIEDI DEBRIEFING COACH (Analisi Telemetrica)"):
                if st.button("Genera Analisi Post-Allenamento", use_container_width=True):
                    with st.spinner("Decriptazione telemetria Garmin in corso..."):
                        act_raw = df_attivita.iloc[0].get('raw_data', {})
                        
                        # Estrazione metriche avanzate (con conversioni ingegneristiche)
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
                        Sei "The Notorious", il Coach Olimpionico. 
                        Analizza la telemetria dell'ULTIMO ALLENAMENTO APPENA CONCLUSO del tuo atleta Francesco (40 anni, obiettivo fisico ibrido spartano).
                        
                        DATI TELEMETRICI ESTRATTI DA GARMIN:
                        - Disciplina: {str(df_attivita.iloc[0].get('tipo_sport', 'N/D')).replace('_', ' ').title()}
                        - Titolo Attività: {df_attivita.iloc[0].get('titolo_allenamento', 'Generico')}
                        - Durata: {df_attivita.iloc[0].get('durata_minuti', 'N/D')} minuti
                        - Distanza: {distanza_km} km (se N/D, era un allenamento stazionario)
                        - Passo Medio: {passo_min_km} (se N/D, ignora)
                        - FC Media: {df_attivita.iloc[0].get('fc_media', 'N/D')} bpm
                        - FC Max: {df_attivita.iloc[0].get('fc_max', 'N/D')} bpm
                        - Training Effect Aerobico: {df_attivita.iloc[0].get('training_effect_aerobico', 'N/D')}
                        - Training Effect Anaerobico: {df_attivita.iloc[0].get('training_effect_anaerobico', 'N/D')}
                        
                        LA TUA MISSIONE:
                        Fai un "Debriefing" tecnico e spietato. Valuta se l'intensità (FC, Passo, TE) è coerente con il Titolo dell'Attività (es. se è una "Recovery Run" i battiti dovevano essere bassi; se sono "Ripetute" mi aspetto un TE Anaerobico alto e FC Max elevata; se è "Pesi" ignora il passo). 
                        
                        FORMAT OBBLIGATORIO:
                        - 🎯 **VALUTAZIONE ESECUTIVA:** (Giudizio netto in 2 righe).
                        - 📈 **ANALISI MOTORE:** (Analisi dei battiti, del passo o dell'impatto sul sistema cardiovascolare/nervoso in base al Training Effect).
                        - ⚠️ **VERDETTO DEL COACH:** (Un consiglio tagliente su come gestire il recupero o il prossimo allenamento).
                        """
                        
                        try:
                            risposta_debrief = client.chat.completions.create(
                                model="gpt-4o-mini",
                                messages=[{"role": "system", "content": prompt_debriefing}],
                                temperature=0.6
                            )
                            st.info(risposta_debrief.choices[0].message.content)
                        except Exception as e:
                            st.error(f"Errore di connessione al motore AI: {e}")

        st.markdown("---")

        # --- 3. I 4 GRAFICI GOD MODE (Bug Risolto) ---

        col_top1, col_top2 = st.columns(2)
        
        with col_top1:
            bb_attuale_val = ultima_riga.get('body_battery_attuale')
            if pd.isna(bb_attuale_val): bb_attuale_val = ultima_riga.get('body_battery_max', 0) 
            fig_gauge = go.Figure(go.Indicator(
                mode = "gauge+number", value = bb_attuale_val,
                title = {'text': "SERBATOIO ENERGETICO (Body Battery)"},
                gauge = {
                    'axis': {'range': [0, 100]}, 'bar': {'color': "#3b82f6"},
                    'steps': [{'range': [0, 30], 'color': "#ff4b4b"}, {'range': [30, 70], 'color': "#ffa600"}, {'range': [70, 100], 'color': "#00cc96"}]
                }
            ))
            st.plotly_chart(fig_gauge, use_container_width=True)

        with col_top2:
            comp_data = pd.DataFrame({
                'Componente': ['Massa Muscolare', 'Massa Grassa', 'Massa Ossea', 'Grasso Viscerale (Indice)', 'Acqua Corporea'],
                'Valori': [ultima_riga.get('muscoli_kg', 0), ultima_riga.get('massa_grassa_kg', 0), ultima_riga.get('massa_ossea_kg', 0), ultima_riga.get('grasso_viscerale', 0), ultima_riga.get('acqua_kg', 0)]
            })
            comp_data = comp_data[comp_data['Valori'] > 0]
            fig_tree = px.treemap(comp_data, path=['Componente'], values='Valori', title="STRUTTURA CORPOREA (kg)", color='Valori', color_continuous_scale='Reds')
            st.plotly_chart(fig_tree, use_container_width=True)

        col_bot1, col_bot2 = st.columns(2)

        with col_bot1:
            fig_scatter = px.scatter(
                df_master, x="sonno_ore", y="rhr", size="body_battery_max", color="stress_medio",
                title="EFFICIENZA RECUPERO (Sonno vs RHR)",
                labels={"sonno_ore": "Ore di Sonno", "rhr": "Battiti a Riposo (RHR)"}, size_max=20,
                color_continuous_scale=px.colors.sequential.Reds
            )
            st.plotly_chart(fig_scatter, use_container_width=True)

        with col_bot2:
            if not df_attivita.empty:
                df_grafico_att = df_attivita.head(7).copy()
                
                df_grafico_att['data'] = pd.to_datetime(df_grafico_att['data']).dt.strftime('%d/%m')
                
                if 'titolo_allenamento' not in df_grafico_att.columns:
                    df_grafico_att['titolo_allenamento'] = 'Generico'

                fig_bar = px.bar(
                    df_grafico_att, x="data", y="durata_minuti", color="tipo_sport",
                    text="titolo_allenamento",
                    title="VOLUME DI FUOCO (Ultime 7 Attività)",
                    labels={"durata_minuti": "Durata (min)", "data": "Data", "tipo_sport": "Disciplina"},
                    color_discrete_sequence=px.colors.qualitative.Set1
                )
                fig_bar.update_traces(textposition='inside', textfont_size=10)
                st.plotly_chart(fig_bar, use_container_width=True)
            else:
                st.info("Nessuna attività sufficiente per generare il grafico del volume.")

# ==========================================
# TAB 2: IL COACH AI (Dinamico e Blindato)
# ==========================================
with tab_coach:
    st.header("🧠 GENERAZIONE PROTOCOLLO")
    
    col_scelta1, col_scelta2 = st.columns(2)
    with col_scelta1:
        sport_scelto = st.selectbox("🎯 Disciplina Odierna:", ["Seleziona...", "🏃 Corsa", "🏋️ Sala Pesi", "🚴 Ciclismo"])
    with col_scelta2:
        doms_level = st.slider("🔥 Livello DOMS", min_value=0, max_value=10, value=0)

    st.markdown("---")
    
    if sport_scelto != "Seleziona...":
        if st.button(f"GENERA PROTOCOLLO: {sport_scelto}", type="primary", use_container_width=True):
            if df_master.empty:
                st.error("Dati mancanti. Esegui prima una sincronizzazione Garmin.")
            else:
                with st.spinner(f'Elaborazione balistica in corso per {sport_scelto}...'):
                    
                    oggi = pd.Timestamp(date.today())
                    
                    # --- BUGFIX: CALCOLO INIZIO SETTIMANA ---
                    # In Pandas, dayofweek lunedì=0, domenica=6. 
                    # Vogliamo che la settimana inizi la Domenica, quindi se oggi è domenica consideriamo oggi stesso, 
                    # altrimenti andiamo indietro fino alla domenica precedente.
                    giorni_da_sottrarre = (oggi.dayofweek + 1) % 7 
                    inizio_settimana = oggi - pd.Timedelta(days=giorni_da_sottrarre)
                    
                    bb_attuale_energia = df_master.iloc[-1].get('body_battery_attuale', 'N/D')
                    peso_attuale = df_master.iloc[-1].get('peso_kg', 75.0)
                    muscoli_attuali = df_master.iloc[-1].get('muscoli_kg', 55.0)
                    
                    if not df_attivita.empty:
                        df_attivita['data'] = pd.to_datetime(df_attivita['data'])
                        
                        def estrai_nome_garmin(raw):
                            if isinstance(raw, dict):
                                return raw.get('activityName', 'Generico')
                            return 'Generico'
                            
                        df_attivita['titolo_allenamento'] = df_attivita['raw_data'].apply(estrai_nome_garmin)
                        
                        # Filtriamo lo storico inviato all'AI ESATTAMENTE a partire dall'inizio_settimana calcolato
                        storico_attivita = df_attivita[df_attivita['data'] >= inizio_settimana].drop(columns=['raw_data'], errors='ignore').to_dict(orient="records")
                    else:
                        storico_attivita = "Nessun allenamento"
                    
                    base_context = f"""
                    Sei "The Notorious", il Coach Olimpionico estremo. Alleni Francesco, 40 anni, Ingegnere Gestionale, Data & Process Analyst, Business Intelligence. 
                    Peso: {peso_attuale} kg. Massa muscolare: {muscoli_attuali} kg.
                    TELEMETRIA OGGI: Body Battery: {bb_attuale_energia}%. DOMS: {doms_level}/10. 
                    """

                    if sport_scelto == "🏃 Corsa":
                        prompt_di_sistema = base_context + """
                        DISCIPLINA SELEZIONATA: CORSA SULL'ASFALTO. 
                        ATTENZIONE: NON citare, NON inserire e NON nominare nessun esercizio di sala pesi, né addominali, né core, né altro che non sia di pertinenza al running.
                        
                        REGOLE MATEMATICHE INVIOLABILI: 
                        1. DISTANZA TOTALE: L'allenamento totale deve essere SEMPRE compreso in una forbice tra i 10 km e i 14 km. 
                        Questo calcolo deve includere categoricamente: Riscaldamento + Lavoro Centrale + Defaticamento. 
                        2. RISCALDAMENTO SPECIFICO: Solo ed esclusivamente corsa lenta, mobilità articolare o allunghi. Vietato inserire plank o crunch o altro che non sia di pertinenza al running.
                        3. POLARIZZAZIONE 80/20: Leggi lo storico dell'atleta. Se l'ultima corsa era intensa, imponi una Z2 (Recovery Run). Se è fresco, aggancia ripetute, fartlek o tempo run.
                        
                        FORMAT OBBLIGATORIO:
                        - 📊 **[TELEMETRIA INGAGGIATA]**
                        - 🧠 **[STRATEGIA]** (Spiega la logica basata sullo storico)
                        - ⚙️ **[PROTOCOLLO OPERATIVO]** (Fase 1 Riscaldamento, Fase 2 Lavoro Centrale, Fase 3 Defaticamento. Evidenzia la somma matematica dei km totali).
                        - 🥩 **[BIO-HACKING NUTRIZIONALE]**
                        """
                        
                    elif sport_scelto == "🏋️ Sala Pesi":
                        prompt_di_sistema = base_context + f"""
                        DISCIPLINA SELEZIONATA: SALA PESI (OLYMPIC HYBRID MODE).
                        OBIETTIVO: Fisico monumentale, spartano, ibrido definitivo (centometrista/lottatore). Forza esplosiva da vendere, ipertrofia densa e condizionamento letale per dominare l'Hyrox e la corsa.
                        
                        REGOLE STRUTTURALI INVIOLABILI:
                        1. MAX 60 MINUTI: L'intero allenamento deve rientrare in 60 minuti netti. Cronometro alla mano.
                        2. MOTORE DI VARIANZA BIOMECCANICA (CRUCIALE): L'atleta si sta abituando perché gli stai proponendo sempre Panca/Trazioni/Squat/Stacco. DEVI VARIARE SEMPRE GLI ANGOLI DI LAVORO rispetto alla settimana precedente. 
                           - Varianti Upper Spinta: Alterna Panca piana bilanciere, Panca Inclinata manubri, Floor press, Dips zavorrate, Spinte su declinata, Push press.
                           - Varianti Upper Tirata: Alterna Trazioni (prona/supina), Rematore bilanciere, Rematore manubri, Seal Row, Pulley basso, Lat machine triangolo.
                           - Varianti Lower: Alterna Squat classico, Front Squat, Hack Squat, Bulgarian Split Squat, Stacco Trap Bar, Stacco Rumeno, Leg Press pesante.
                           Sii spietato, creativo e imprevedibile. Sorprendi l'atleta.
                        3. IL CONFINAMENTO SETTIMANALE: Il microciclo di allenamento che riceverai (la variabile '{storico_attivita}') inizia a partire dalla Domenica e rappresenta ESCLUSIVAMENTE la settimana di allenamento in corso. 
                        4. LA SEQUENZA SPARTANA RIGIDA (A -> B -> C): Devi rispettare la rotazione all'interno di questa specifica settimana in corso. Analizza minuziosamente i titoli Garmin forniti:
                           - Se nello storico non c'è NESSUN allenamento pesi: INIZIA DA TIPO A (Upper).
                           - Se c'è solo un TIPO A: Imponi TIPO B (Lower).
                           - Se ci sono sia TIPO A che TIPO B: Chiudi con TIPO C (Metabolic).
                        
                        ARCHITETTURA OBBLIGATORIA DELLE SESSIONI:
                        Il tuo output DEVE essere tassativamente diviso in queste 3 Fasi specifiche:
                        
                        [PER IL TIPO B - LOWER BODY & CORE]:
                        - Fase 1: Esplosività Pura (Cambia sempre attrezzo: Box Jump, Broad Jump, Kettlebell Swing, o Cleans). 3-4 serie, poche rep, esplosione massima.
                        - Fase 2: Forza Pesante (Applica il Motore di Varianza per scegliere il multiarticolare. 4-5 serie, 5-8 rep).
                        - Fase 3: Core Anti-Rotazionale e Stabilità (Es. Plank zavorrato, Pallof Press, Farmer's Walk, o Ab-Roll).
                        
                        [PER IL TIPO A - UPPER BODY]:
                        - Fase 1: Esplosività Neurale (Es. Plyo push-ups, Lanci esplosivi).
                        - Fase 2: Forza Base (Applica il Motore di Varianza per 1 Spinta Orizzontale/Verticale e 1 Tirata Orizzontale/Verticale).
                        - Fase 3: Ipertrofia Accessoria (Braccia e Spalle con cavi o manubri, alta intensità).
                        
                        [PER IL TIPO C - METABOLIC GOD MODE (HYROX PREP)]:
                        - Un singolo blocco massacrante (Circuito EMOM, AMRAP, o For Time) da 35-45 min. Mescola sempre: esercizi di spinta (Thruster, Wall ball), trazione (Rematori, trazioni), gambe (Affondi, Box step-up) e condizionamento (Burpees, Corsa sul posto, Rowing).
                        
                        FORMAT OBBLIGATORIO DEL TUO OUTPUT:
                        - 📊 **[TELEMETRIA INGAGGIATA]**
                        - 🧠 **[STRATEGIA OLYMPIC HYBRID]** (Spiega a che punto della sequenza si trova. SOTTOLINEA le variazioni biomeccaniche che hai inserito oggi rispetto ai classici multiarticolari per shockare il muscolo).
                        - ⚙️ **[PROTOCOLLO OPERATIVO - MAX 60 MINUTI]** (Scrivi "Fase 1", "Fase 2", "Fase 3" con esercizi NUOVI, serie, rep e recuperi. Per il Tipo C scrivi il Circuito completo).
                        - 🥩 **[BIO-HACKING NUTRIZIONALE]**
                        """
                        
                    elif sport_scelto == "🚴 Ciclismo":
                        prompt_di_sistema = base_context + """
                        DISCIPLINA SELEZIONATA: CICLISMO.
                        ATTENZIONE: NON citare, NON inserire e NON nominare nessun esercizio di sala pesi, né addominali, né core.
                        
                        REGOLE INVIOLABILI:
                        1. CROSS-TRAINING: Impatto articolare zero. Il riscaldamento deve essere solo pedalata agile.
                        2. LOGICA: Se l'atleta ha DOMS alti o ha accumulato molti km a piedi, imponi un "Active Recovery" (95-100 RPM, Z1/Z2) per lavare l'acido lattico. Se l'atleta è fresco, prescrivi intervalli VO2 Max o Sweet Spot (%FTP / Watt).
                        
                        FORMAT OBBLIGATORIO:
                        - 📊 **[TELEMETRIA INGAGGIATA]**
                        - 🧠 **[STRATEGIA]** 
                        - ⚙️ **[PROTOCOLLO OPERATIVO]**
                        - 🥩 **[BIO-HACKING NUTRIZIONALE]**
                        """

                    prompt_utente = f"Microciclo della settimana: {storico_attivita}."
                    
                    try:
                        risposta = client.chat.completions.create(
                            model="gpt-4o-mini",
                            messages=[{"role": "system", "content": prompt_di_sistema}, {"role": "user", "content": prompt_utente}],
                            temperature=0.7
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
