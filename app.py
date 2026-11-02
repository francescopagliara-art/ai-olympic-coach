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

st.set_page_config(page_title="Protocollo I90 | The Notorious OS", page_icon="🐺", layout="wide")

# ==========================================
# 2. MOTORE DI SINCRONIZZAZIONE (GARMIN -> SUPABASE)
# ==========================================
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

        # --- Preserva eventuale tracciamento abitudini già salvato oggi ---
        mindset_salvato = None
        res_esistente = requests.get(f"{supabase_url}/rest/v1/metrica_giornaliera?data=eq.{oggi}&select=raw_data", headers=headers_get)
        if res_esistente.status_code == 200 and len(res_esistente.json()) > 0:
            raw_esistente = res_esistente.json()[0].get("raw_data")
            if isinstance(raw_esistente, dict) and "i90_mindset" in raw_esistente:
                mindset_salvato = raw_esistente["i90_mindset"]

        # --- A. Estrazione Metriche Giornaliere ---
        stats = garmin.get_stats(oggi)
        if not isinstance(stats, dict):
            stats = {}
        if mindset_salvato:
            stats["i90_mindset"] = mindset_salvato

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

        # --- B. Estrazione Ultime Attività Sportive ---
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
# 3. MOTORE DI LETTURA DATI (ETL DA SUPABASE)
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

# ==========================================
# 4. INTERFACCIA: BARRA LATERALE E STATO I90
# ==========================================
with st.sidebar:
    st.header("⚡ TERMINALE I90")
    if st.button("🔄 SINCRONIZZA GARMIN", type="primary", use_container_width=True):
        with st.spinner("Estrazione telemetria in corso..."):
            if sincronizza_garmin_completo():
                st.success("Sincronizzazione completata!")
                st.cache_data.clear()
                st.rerun()
                
    st.markdown("---")
    st.subheader("📅 Periodizzazione 90 giorni")
    settimana_i90 = st.slider("Settimana del protocollo", min_value=1, max_value=12, value=1)
    
    if settimana_i90 <= 4:
        blocco_nome = "Blocco 1: Fondamenta"
        blocco_focus = "Tecnica sui fondamentali, costruzione base aerobica in Zona 2, primi circuiti metabolici."
    elif settimana_i90 <= 8:
        blocco_nome = "Blocco 2: Costruzione"
        blocco_focus = "Aumento del volume e dei carichi (80-85%), corse lunghe progressive, ripetute in soglia, blocchi finali EMOM."
    else:
        blocco_nome = "Blocco 3: Picco"
        blocco_focus = "Intensità massimale (85-92%), ritmo gara specifico, capacità di lavoro al limite prima del test finale."
        
    is_scarico = settimana_i90 in [4, 8, 12]
    stato_ciclo = "🟢 SCARICO (3:1) - Recupero e supercompensazione (-30% volume)" if is_scarico else "🔥 SPINTA (3:1) - Carico progressivo"
    
    st.info(f"**{blocco_nome}**\n\n{stato_ciclo}\n\n*Focus:* {blocco_focus}")

# ==========================================
# 5. TITOLO E SCHEDE
# ==========================================
st.title("🐺 Protocollo I90 - Sistema operativo ibrido")
st.markdown("*Forza. Resistenza. Disciplina sulle 24 ore. Zero interferenza.*")
st.markdown("---")

tab_dashboard, tab_coach, tab_mindset, tab_inserimento = st.tabs([
    "📊 TELEMETRIA", 
    "⚔️ALLENAMENTO I90", 
    "🧠 LE 4 REGOLE (ON/OFF)", 
    "⚖️ COMPOSIZIONE CORPOREA"
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
        col1.metric("Ore di sonno", f"{sonno_val} h", help="Ore totali di sonno registrate l'ultima notte.")
        
        fc = ultima_riga.get('rhr')
        fc_val = int(fc) if pd.notna(fc) else "N/D"
        col2.metric("FC a riposo", f"{fc_val} bpm", help="Battiti medi a riposo. Indicatore chiave dell'efficienza aerobica e del recupero.")
        
        calorie = raw.get('totalKilocalories')
        cal_val = int(calorie) if pd.notna(calorie) else "N/D"
        col3.metric("Calorie totali", f"{cal_val} kcal", help="Stima delle calorie totali bruciate finora nella giornata odierna.")
        
        stress = ultima_riga.get('stress_medio')
        stress_val = int(stress) if pd.notna(stress) else "N/D"
        col4.metric("Stress medio", f"{stress_val} / 100", help="Tensione del sistema nervoso autonomo.")
        st.markdown("---")

        st.subheader("🏅 Ultima sessione registrata")
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
        else:
            st.info("Nessuna attività registrata.")
            
        if not df_attivita.empty:
            with st.expander("🎙️ RICHIEDI ANALISI TECNICA POST-ALLENAMENTO"):
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
                        Analizza la telemetria dell'ultimo allenamento concluso da Francesco (40 anni, atleta ibrido: forza sui fondamentali + corsa).
                        
                        DATI TELEMETRICI GARMIN:
                        - Disciplina: {str(df_attivita.iloc[0].get('tipo_sport', 'N/D')).replace('_', ' ').title()}
                        - Titolo attività: {df_attivita.iloc[0].get('titolo_allenamento', 'Generico')}
                        - Durata: {df_attivita.iloc[0].get('durata_minuti', 'N/D')} minuti
                        - Distanza: {distanza_km} km
                        - Passo medio: {passo_min_km}
                        - FC media: {df_attivita.iloc[0].get('fc_media', 'N/D')} bpm
                        - FC massima: {df_attivita.iloc[0].get('fc_max', 'N/D')} bpm
                        - Training Effect aerobico: {df_attivita.iloc[0].get('training_effect_aerobico', 'N/D')}
                        - Training Effect anaerobico: {df_attivita.iloc[0].get('training_effect_anaerobico', 'N/D')}
                        
                        Valuta con rigore scientifico se lo stimolo registrato rispetta l'isolamento dell'interferenza del Protocollo I90 (es. se era una corsa in Zona 2 i battiti dovevano restare bassi sotto i 145 bpm; se erano ripetute doveva esserci uno stimolo anaerobico netto).
                        
                        Struttura obbligatoria:
                        - 🎯 **Valutazione esecutiva:** giudizio diretto in 2 righe.
                        - 📈 **Analisi fisiologica:** impatto su motore aerobico o sistema nervoso centrale.
                        - ⚠️ **Direttiva per domani:** come gestire il recupero in vista della sessione di domani del calendario I90.
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

        st.markdown("---")

        col_top1, col_top2 = st.columns(2)
        with col_top1:
            bb_attuale_val = ultima_riga.get('body_battery_attuale')
            if pd.isna(bb_attuale_val):
                bb_attuale_val = ultima_riga.get('body_battery_max', 0)
            fig_gauge = go.Figure(go.Indicator(
                mode="gauge+number", value=bb_attuale_val if pd.notna(bb_attuale_val) else 0,
                title={'text': "SERBATOIO ENERGETICO (Body Battery)"},
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
                    fig_tree = px.treemap(comp_data, path=['Componente'], values='Valori', title="STRUTTURA CORPOREA (kg)", color='Valori', color_continuous_scale='Reds')
                    st.plotly_chart(fig_tree, use_container_width=True)

        col_bot1, col_bot2 = st.columns(2)
        with col_bot1:
            fig_scatter = px.scatter(
                df_master, x="sonno_ore", y="rhr", size="body_battery_max", color="stress_medio",
                title="EFFICIENZA RECUPERO (Sonno vs FC a riposo)",
                labels={"sonno_ore": "Ore di sonno", "rhr": "Battiti a riposo"}, size_max=20,
                color_continuous_scale=px.colors.sequential.Reds
            )
            st.plotly_chart(fig_scatter, use_container_width=True)

        with col_bot2:
            if not df_attivita.empty:
                df_grafico_att = df_attivita.head(7).copy()
                df_grafico_att['data'] = pd.to_datetime(df_grafico_att['data']).dt.strftime('%d/%m')
                fig_bar = px.bar(
                    df_grafico_att, x="data", y="durata_minuti", color="tipo_sport",
                    text="titolo_allenamento",
                    title="VOLUME DI LAVORO (Ultime 7 attività)",
                    labels={"durata_minuti": "Durata (min)", "data": "Data", "tipo_sport": "Disciplina"},
                    color_discrete_sequence=px.colors.qualitative.Set1
                )
                fig_bar.update_traces(textposition='inside', textfont_size=10)
                st.plotly_chart(fig_bar, use_container_width=True)

# ==========================================
# SCHEDA 2: ALLENAMENTO PROTOCOLLO I90
# ==========================================
with tab_coach:
    st.header("⚔️ Generatore sessione Protocollo I90")
    st.caption("Isolamento dell'interferenza: 3 sedute di forza e 3 di corsa a settimana, mai nello stesso giorno.")
    
    mappa_giorni_i90 = {
        0: "1. Lunedì — Recupero attivo o Duathlon indoor (Bici + Corsa Z2)",
        1: "2. Martedì — Forza Parte Superiore (Fondamentali: Panca, Trazioni zavorrate, Military)",
        2: "3. Mercoledì — Corsa: Ripetute e Soglia (Motore anaerobico e VO2 Max)",
        3: "4. Giovedì — Forza Parte Inferiore (Fondamentali: Squat, Stacco + Catena cinetica)",
        4: "5. Venerdì — Corsa: Easy Run in Zona 2 (Costruzione capillare e recupero)",
        5: "6. Sabato — Condizionamento Ibrido (Full Body + Circuito Metcon stile Hyrox)",
        6: "7. Domenica — Corsa: Long Run progressivo (Resistenza aerobica strutturale)"
    }
    
    giorno_odierno_idx = date.today().weekday()
    opzioni_i90 = list(mappa_giorni_i90.values())
    
    col_s1, col_s2 = st.columns([2, 1])
    with col_s1:
        modulo_scelto = st.selectbox(
            "🎯 Modulo giornaliero I90 (preimpostato sul giorno odierno):",
            opzioni_i90,
            index=giorno_odierno_idx
        )
    with col_s2:
        doms_level = st.slider("🔥 Affaticamento muscolare (DOMS)", min_value=0, max_value=10, value=2)

    st.markdown("---")
    
    if st.button(f"GENERA SESSIONE UFFICIALE I90", type="primary", use_container_width=True):
        if df_master.empty:
            st.error("Dati mancanti. Esegui prima una sincronizzazione Garmin.")
        else:
            with st.spinner("Calcolo parametri di carico e periodizzazione I90 in corso..."):
                oggi_ts = pd.Timestamp(date.today())
                inizio_settimana = oggi_ts - pd.Timedelta(days=oggi_ts.dayofweek)
                
                ultima = df_master.iloc[-1]
                bb_attuale_energia = ultima.get('body_battery_attuale', 'N/D')
                rhr_attuale = ultima.get('rhr', 'N/D')
                peso_attuale = ultima.get('peso_kg', 75.0)
                muscoli_attuali = ultima.get('muscoli_kg', 55.0)
                
                if not df_attivita.empty:
                    df_attivita['data_dt'] = pd.to_datetime(df_attivita['data'])
                    df_attivita['titolo_allenamento'] = df_attivita['raw_data'].apply(
                        lambda r: r.get('activityName', 'Generico') if isinstance(r, dict) else 'Generico'
                    )
                    storico_settimana = df_attivita[df_attivita['data_dt'] >= inizio_settimana].drop(
                        columns=['raw_data', 'data_dt'], errors='ignore'
                    ).to_dict(orient="records")
                else:
                    storico_settimana = "Nessuna attività registrata questa settimana"

                prompt_i90 = f"""
                Sei il Direttore Tecnico del "Protocollo I90", il sistema operativo di 90 giorni per l'atleta ibrido definitivo ispirato alla metodologia di Marco Tomasin.
                Alleni Francesco (40 anni, Ingegnere Gestionale, peso {peso_attuale} kg, massa muscolare {muscoli_attuali} kg).
                
                STATO ATTUALE DEL SISTEMA OPERATIVO (PERIODIZZAZIONE 3:1):
                - Settimana corrente: Settimana {settimana_i90} su 12 ({blocco_nome})
                - Fase del ciclo 3:1: {stato_ciclo}
                - Focus del blocco: {blocco_focus}
                - Telemetria odierna: Body Battery {bb_attuale_energia}%, FC riposo {rhr_attuale} bpm, DOMS {doms_level}/10.
                - Storico attività della settimana corrente: {storico_settimana}
                
                MODULO ODIERNO SELEZIONATO DALL'ATLETA:
                {modulo_scelto}
                
                PRINCIPI SCIENTIFICI INVIOLABILI DEL PROTOCOLLO I90:
                1. ISOLAMENTO DELL'INTERFERENZA: Nei giorni di forza (Martedì e Giovedì) è VIETATO inserire corsa o circuiti cardio lunghi. Nei giorni di corsa (Mercoledì, Venerdì, Domenica) è VIETATO inserire esercizi con i pesi o addominali. Forza e resistenza si fondono SOLO il Sabato.
                2. PERIODIZZAZIONE 3:1 REALE:
                   - Se siamo in settimana di SCARICO (Settimana 4, 8 o 12): riduci le serie del 30%, tieni un margine più ampio (RIR 3) e accorcia i chilometraggi per permettere la supercompensazione.
                   - Se siamo nel Blocco 1 (Sett. 1-3): focus su controllo tecnico, fermo al petto/buca, range 6-8 ripetizioni sui fondamentali (75-80% 1RM).
                   - Se siamo nel Blocco 2 (Sett. 5-7): focus costruzione e volume, range 5-6 ripetizioni pesanti (80-85% 1RM) + complementari densi.
                   - Se siamo nel Blocco 3 (Sett. 9-11): focus picco di forza e potenza, range 3-5 ripetizioni (85-90% 1RM) e ritmi gara serrati.
                3. REGOLE SPECIFICHE PER IL MODULO ODIERNO:
                   - Se è MARTEDÌ (Forza Upper Body): Costruisci la scheda sui Fondamentali (Panca piana con bilanciere, Trazioni alla sbarra zavorrate, Military Press con bilanciere, Rematore pesante) seguiti da 2 complementari mirati per braccia e deltoidi in super serie. Durata 50-60 minuti, recuperi completi (120s) sui fondamentali per massimizzare il reclutamento neurale senza bruciare il sistema nervoso per le ripetute di domani.
                   - Se è MERCOLEDÌ (Ripetute Corsa): Struttura riscaldamento (2-3 km Z1/Z2 + allunghi), lavoro centrale di ripetute (es. 6-8x1000m o 10x400m o piramidale in base al Blocco 1/2/3) con tempi di recupero precisi, e defaticamento. Totale 10-12 km.
                   - Se è GIOVEDÌ (Forza Lower Body): Costruisci la scheda sui Fondamentali (Squat con bilanciere e Stacco da terra / Stacco rumeno pesante), seguiti da lavoro unilaterale (Affondi bulgari o step-up pesanti) e Core anti-rotazionale (Plank zavorrato, Pallof press, Ab-wheel). Niente cedimento metabolico estremo che impedisca di correre venerdì e domenica.
                   - Se è VENERDÌ (Easy Run Zona 2): Imponi una corsa rigorosamente lenta tra i 10 e i 12 km. Specifica la fascia cardiaca (Zona 2, conversazionale, circa 130-145 bpm) spiegando che serve a costruire i capillari e ad assorbire il Lower Body di ieri.
                   - Se è SABATO (Full Body + Circuito Funzionale / Hybrid Conditioning): Questo è il giorno in cui forza e fiato si fondono. Fase 1 (20 min): Richiamo di potenza/forza full body (es. Clean & Press, Front Squat o Truster pesanti). Fase 2 (25-30 min): Il Metcon settimanale in stile Hyrox/CrossFit (AMRAP, EMOM o For Time spietato con Burpees, Affondi in camminata con manubri, Trazioni, Devil Press, Farmer's carry, Wall ball).
                   - Se è DOMENICA (Long Run): Corsa lunga progressiva strutturata in base al Blocco (da 12 km nel Blocco 1 fino a 16-18 km nel Blocco 3, oppure 10 km rigeneranti in settimana di scarico).
                   - Se è LUNEDÌ (Recupero o Duathlon Indoor): Proponi un protocollo di mobilità articolare profonda + 35-45 minuti di lavoro aerobico incrociato a impatto zero in Zona 1/2 (Bici + corsa leggera) per lavare le scorie.

                FORMATO OBBLIGATORIO DELL'OUTPUT:
                - 📊 **[INQUADRAMENTO TATTICO I90]** (Indica Settimana, Blocco, stato Spinta/Scarico e lettura della telemetria).
                - ⚙️ **[PROTOCOLLO OPERATIVO DI OGGI]** (Dettaglio chirurgico di serie, ripetizioni, % di carico o RIR, tempi di recupero o passi al km).
                - 🧠 **[MINDSET E STANDARD NON NEGOZIABILE]** (Un richiamo secco e diretto alla legge "FATTO o NON FATTO" e alle 4 regole quotidiane).
                """
                
                try:
                    risposta = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "system", "content": prompt_i90}],
                        temperature=0.6
                    )
                    st.success("PROTOCOLLO I90 GENERATO CON SUCCESSO.")
                    st.markdown(risposta.choices[0].message.content)
                except Exception as e:
                    st.error(f"Errore API OpenAI: {e}")

# ==========================================
# SCHEDA 3: LO STILE DI VITA (LE 4 REGOLE ON/OFF)
# ==========================================
with tab_mindset:
    st.header("🧠 Le 4 regole quotidiane — Legge binaria: FATTO o NON FATTO")
    st.write("Un sistema operativo non vive solo in palestra, vive nelle 24 ore. Ogni regola è un interruttore: **ON** o **OFF**. Niente 'fatto a metà'.")
    
    oggi_str = date.today().isoformat()
    
    # Lettura stato odierno se già presente
    stato_Iniziale = {"regola_1": False, "regola_2": False, "regola_3": False, "regola_4": False, "vittorie": "", "piano_domani": ""}
    raw_oggi = {}
    if not df_master.empty:
        riga_oggi = df_master[df_master['data'] == pd.Timestamp(oggi_str)]
        if not riga_oggi.empty:
            raw_temp = riga_oggi.iloc[-1].get('raw_data', {})
            if isinstance(raw_temp, dict):
                raw_oggi = raw_temp
                if "i90_mindset" in raw_oggi:
                    stato_Iniziale.update(raw_oggi["i90_mindset"])

    with st.form("form_mindset_i90"):
        st.subheader(f"Registro di disciplina del {date.today().strftime('%d/%m/%Y')}")
        
        c1, c2 = st.columns(2)
        with c1:
            r1 = st.checkbox(
                "1. 🥩 Alimentazione pulita (Zero alcol, zero cibo spazzatura, zero calorie liquide)",
                value=stato_Iniziale["regola_1"]
            )
            r2 = st.checkbox(
                "2. 📖 Allenamento mentale (Almeno 10 pagine di saggistica: business, psicologia, biografie)",
                value=stato_Iniziale["regola_2"]
            )
        with c2:
            r3 = st.checkbox(
                "3. 🎯 Programma il domani (Rituale serale Navy SEAL: 3 vittorie + piano di domani)",
                value=stato_Iniziale["regola_3"]
            )
            r4 = st.checkbox(
                "4. ❄️ Rituale di scomodità (Doccia fredda di almeno 1 minuto appena sveglio)",
                value=stato_Iniziale["regola_4"]
            )
            
        st.markdown("---")
        col_t1, col_t2 = st.columns(2)
        with col_t1:
            vittorie_txt = st.text_area("🏆 Le 3 vittorie di oggi:", value=stato_Iniziale["vittorie"], height=100)
        with col_t2:
            piano_txt = st.text_area("🗺️ Pianificazione chirurgica di domani:", value=stato_Iniziale["piano_domani"], height=100)
            
        salva_mindset = st.form_submit_button("REGISTRA GIORNATA SUL DATABASE", type="primary", use_container_width=True)
        
        if salva_mindset:
            fatto_totale = bool(r1 and r2 and r3 and r4)
            raw_oggi["i90_mindset"] = {
                "regola_1": r1,
                "regola_2": r2,
                "regola_3": r3,
                "regola_4": r4,
                "vittorie": vittorie_txt,
                "piano_domani": piano_txt,
                "esito_binario": "FATTO" if fatto_totale else "NON FATTO"
            }
            headers_upsert = {
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates"
            }
            try:
                requests.post(
                    f"{supabase_url}/rest/v1/metrica_giornaliera",
                    headers=headers_upsert,
                    json={"data": oggi_str, "raw_data": raw_oggi}
                ).raise_for_status()
                st.cache_data.clear()
                if fatto_totale:
                    st.success("🟢 STATO GIORNALIERO: FATTO (4/4 ON). Disciplina d'élite confermata.")
                else:
                    st.error("🔴 STATO GIORNALIERO: NON FATTO (OFF). Non esistono mezze misure: completa i pilastri mancanti.")
            except Exception as e:
                st.error(f"Errore durante il salvataggio: {e}")

    # Calcolo storico conformità binaria
    if not df_master.empty and 'raw_data' in df_master.columns:
        giorni_tracciati = 0
        giorni_fatti = 0
        for _, row in df_master.tail(14).iterrows():
            rd = row.get('raw_data')
            if isinstance(rd, dict) and 'i90_mindset' in rd:
                giorni_tracciati += 1
                if rd['i90_mindset'].get('esito_binario') == "FATTO":
                    giorni_fatti += 1
        if giorni_tracciati > 0:
            perc_disciplina = round((giorni_fatti / giorni_tracciati) * 100)
            st.metric("Tasso di conformità binaria (ultimi 14 giorni registrati)", f"{perc_disciplina}% ({giorni_fatti}/{giorni_tracciati} giorni ON)")

# ==========================================
# SCHEDA 4: INSERIMENTO MANUALE DATI ZEPP
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
            
        if st.form_submit_button("SALVA DATI SU DATABASE", type="primary"):
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
                st.success("✅ Dati corporei salvati. Sincronizzazione strutturale completata.")
                st.cache_data.clear()
            except Exception as e:
                st.error(e)

# ==========================================
# FOOTER / FIRMA
# ==========================================
st.markdown("---")
st.markdown("<p style='text-align: center; color: gray; font-weight: bold; letter-spacing: 2px;'>ENGINEERED BY FRANCESCO | PROTOCOLLO I90 OPERATING SYSTEM</p>", unsafe_allow_html=True)
