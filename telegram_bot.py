import os
import requests
from openai import OpenAI

# 1. Configurazione Credenziali da GitHub Secrets
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

def genera_messaggio_notorious():
    prompt = """
    Sei il Coach Olimpionico d'élite "The Notorious". La tua filosofia è dominio mentale, leadership, esecuzione impeccabile e zero scuse.
    Scrivi un breve e spietato messaggio motivazionale mattutino (massimo 4-5 righe) per l'atleta Francesco. 
    Il tono deve essere autorevole, ingegneristico e crudo. Ricordagli che la consistenza batte il talento e che oggi è il giorno per dominare il microciclo.
    Non usare cliché banali, usa un linguaggio chirurgico che spinga all'azione immediata.
    """
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )
        return response.choices[0].message.content
    except Exception as e:
        return "Francesco, la linea di partenza è pronta. Muoviti e domina la giornata. Zero scuse."

def invia_notifica_telegram():
    testo_motivazionale = genera_messaggio_notorious()
    
    # Costruzione del testo finale con il link alla tua app Streamlit
    link_app = "https://ai-olympic-coach-wfrbtk5tfkjrppmdyym2xd.streamlit.app/" # Sostituisci con l'URL reale della tua app deployata
    
    messaggio_finale = (
        f"⚡ *THE NOTORIOUS PROTOCOL* ⚡\n\n"
        f"{testo_motivazionale}\n\n"
        f"🔗 *Accedi al Command Center per sincronizzare e generare il tuo protocollo di oggi:* \n{link_app}"
    )
    
    url_telegram = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": messaggio_finale,
        "parse_mode": "Markdown"
    }
    
    res = requests.post(url_telegram, json=payload)
    if res.status_code == 200:
        print("Notifica Telegram inviata con successo.")
    else:
        print(f"Errore invio Telegram: {res.text}")

if __name__ == "__main__":
    invia_notifica_telegram()