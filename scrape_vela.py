"""Agente Vela – classifiche finali delle regate olimpiche (versione 1.3 del 17/11/2026, scheda 35)

Novità della 1.3: LETTORE SAILTI. La piattaforma Sailti (sito del Sailing Grand Slam, Princesa
Sofía, Mondiali iQFOiL...) mostra una classe alla volta: la pagina "resultsall" contiene l'elenco
delle classi e, per ognuna, l'indirizzo "resultsajax" da cui il browser scarica la classifica.
L'agente legge l'elenco, sceglie la classe della riga di eventi_vela.csv e scarica direttamente
quella classifica: niente browser e niente numero di tabella da indicare.
Riconosce anche le colonne tipiche di Sailti: "CREW" (nomi a capo), "SAIL #", posizione senza
titolo, prove "1P", "2P", "1 (US)".

Novità della 1.2: BROWSER AUTOMATICO (Playwright con Chromium, come per l'agente UWW).
Molte pagine (sailing.org, Sailti: Princesa Sofía, Sailing Grand Slam, iQFOiL) arrivano "vuote"
e la classifica la costruisce il browser con JavaScript. Quando una pagina non contiene tabelle,
l'agente la riapre con un browser vero senza finestra, aspetta che la classifica compaia e la
legge da lì. Intanto annota i dati JSON che la pagina scarica (pagine_salvate/json/): se un sito
ha un'interfaccia dati comoda, nelle versioni successive la si potrà leggere direttamente.

Novità della 1.1 (dopo i primi due lanci di prova):
- "Net Score" riconosciuto come punteggio netto (pagine di 49er.org);
- la diagnosi elenca TUTTE le tabelle della pagina e, per ogni classifica, il primo classificato
  (così si capisce di che classe è);
- ogni pagina letta viene salvata in pagine_salvate/ (Artifacts): Claude può studiarla senza
  raggiungere il sito;
- per le pagine senza tabelle (costruite con JavaScript, es. Sailti) la diagnosi elenca iframe,
  menu a tendina, script e indirizzi interni che potrebbero contenere la classifica;
- modalità "scopri": su sailing.org mostra un campione dei link della pagina, per capire perché
  non trova gli eventi.

Che cosa fa
-----------
Per ogni regata elencata nel file eventi_vela.csv legge la CLASSIFICA FINALE COMPLETA di una
classe olimpica (una pagina web per classe) e la scrive nel database Notion "⛵ Agente Vela",
con UNA RIGA PER BARCA PER EVENTO. Queste righe serviranno allo script ELO (sessione 2).

Ha due modalità (input "modalita" del workflow):

  scopri   esplora https://www.sailing.org/events/ e https://www.sailinggrandslam.com e stampa
           tutti i link a pagine di risultati che trova (file eventi_trovati.csv negli Artifacts).
           NON scrive mai nulla su Notion: serve a completare eventi_vela.csv.

  leggi    legge le classifiche elencate in eventi_vela.csv. In "Solo prova" (DRY_RUN=true)
           stampa per ogni pagina una diagnosi (tabelle trovate, colonne riconosciute, prime
           righe, posizioni mancanti, presenza degli ID World Sailing) senza scrivere su Notion.
           Nel lancio vero scrive solo le barche nuove (controllo sulla colonna "ID Univoco").

Perché una diagnosi
-------------------
Ogni regata pubblica i risultati su piattaforme diverse (Sailwave, Sailti, Manage2Sail, pagine
delle classi...). Claude non può aprire questi siti dal suo ambiente, quindi il lettore è
"generico": cerca nella pagina le tabelle HTML che hanno almeno una colonna posizione e una
colonna nome, e riconosce le colonne dal titolo (Rank, Sail No, Helm, Crew, Nat, Net...).
La "Solo prova" dice, pagina per pagina, se il lettore generico basta o serve un lettore apposito.

Variabili d'ambiente (arrivano dal workflow)
--------------------------------------------
NOTION_TOKEN, NOTION_DATABASE_ID   segreti del repository (va bene anche il link intero del db)
DRY_RUN=true                       "Solo prova": non scrive nulla su Notion
MODALITA                           "scopri" oppure "leggi" (default "leggi")
EVENTI                             facoltativo: elenco di id_evento separati da virgola, per
                                   leggere solo quelli (vuoto = tutti gli eventi del file)
TEMPO_MAX_MINUTI                   dopo quanti minuti smettere di scrivere (default 165)
"""
import csv
import datetime as dt
import os
import re
import sys
import time
import traceback
import unicodedata
from collections import Counter
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:                                   # il browser automatico è facoltativo: senza, l'agente
    from playwright.sync_api import sync_playwright   # funziona come la versione 1.1
except ImportError:
    sync_playwright = None

AVVIO = time.monotonic()   # orologio di partenza, per fermarsi prima del limite di GitHub

# ====================================================================== IMPOSTAZIONI
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
# l'id del database si può incollare anche come link intero: ne estraggo i 32 caratteri
_raw_id = os.getenv("NOTION_DATABASE_ID", "").strip().replace("-", "")
_trovato = re.search(r"[0-9a-fA-F]{32}", _raw_id)
DATABASE_ID = _trovato.group(0) if _trovato else _raw_id
DRY_RUN = os.getenv("DRY_RUN", "true").strip().lower() != "false"   # in dubbio: prova
MODALITA = os.getenv("MODALITA", "").strip().lower() or "leggi"
SOLO_EVENTI = {e.strip() for e in os.getenv("EVENTI", "").split(",") if e.strip()}
try:
    TEMPO_MAX_MINUTI = float(os.getenv("TEMPO_MAX_MINUTI", "").strip() or 165)
except ValueError:
    TEMPO_MAX_MINUTI = 165.0

CARTELLA = os.path.dirname(os.path.abspath(__file__))
FILE_EVENTI = os.path.join(CARTELLA, "eventi_vela.csv")
FILE_TROVATI = os.path.join(CARTELLA, "eventi_trovati.csv")
FILE_RAPPORTO = os.path.join(CARTELLA, "rapporto_vela.txt")
CARTELLA_PAGINE = os.path.join(CARTELLA, "pagine_salvate")   # copie delle pagine lette (Artifacts)

PAUSA_NOTION = 0.35   # secondi tra una scrittura e l'altra (Notion accetta circa 3 richieste al secondo)
PAUSA_WEB = 1.0       # secondi tra due pagine dello stesso sito: siamo ospiti, non esageriamo

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124 Safari/537.36")
WEB = requests.Session()
WEB.headers.update({"User-Agent": UA, "Accept-Language": "en,it;q=0.8"})

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",      # stessa versione degli altri agenti
    "Content-Type": "application/json",
}

# Le 10 classi olimpiche, scritte ESATTAMENTE come le opzioni di "Disciplina" in Athletes
# (scheda 34 §2.3): così nella sessione 2 l'abbinamento con Athletes è diretto.
CLASSI = {
    "Men's Laser ILCA 7", "Women's Laser Radial ILCA 6", "Men's iQFOIL", "Women's iQFOIL",
    "Men's 49er", "Women's 49er FX", "Mixed 470", "Nacra 17", "Men's Kite", "Women's Kite",
}
# classi con equipaggio di due persone: timoniere + prodiere (scheda 34, punto 4 del foglio)
DOPPI = {"Men's 49er", "Women's 49er FX", "Mixed 470", "Nacra 17"}

# Tipi di evento ammessi (foglio, punto 7). I coefficienti li usa lo script ELO, non l'agente.
TIPI_EVENTO = {"Olimpiadi", "Mondiali", "Sailing Grand Slam", "Europei",
               "Giochi continentali", "Altre regate"}

# Nomi ESATTI delle colonne del db "⛵ Agente Vela" (scheda 35 §3). Il codice li cerca senza
# badare alle maiuscole. La colonna titolo ("Barca") viene riconosciuta da sola.
COLONNE = {
    "timoniere": ["Timoniere"],
    "prodiere": ["Prodiere"],
    "noc": ["NOC"],
    "id1": ["ID WS 1"],
    "id2": ["ID WS 2"],
    "velico": ["Numero velico"],
    "classe": ["Classe"],
    "posizione": ["Posizione"],
    "punti": ["Punti netti"],
    "barche": ["Barche in classifica"],
    "evento": ["Evento"],
    "tipo": ["Tipo Evento"],
    "inizio": ["Data inizio"],
    "fine": ["Data fine"],
    "fonte": ["Fonte"],
    "note": ["Note"],
    "id": ["ID Univoco"],
}
FACOLTATIVE = {"note"}   # se manca, l'agente lavora lo stesso; tutte le altre sono obbligatorie


# ====================================================================== LOG
_righe_log = []


def log(testo=""):
    """Stampa a video e conserva la riga per il file rapporto_vela.txt (negli Artifacts)."""
    print(testo, flush=True)
    _righe_log.append(str(testo))


def die(msg):
    log(f"ERRORE: {msg}")
    raise SystemExit(1)


def minuti():
    return (time.monotonic() - AVVIO) / 60


# ====================================================================== WEB
def scarica(url):
    """Scarica una pagina. Restituisce (codice HTTP, tipo di contenuto, testo) o solleva un errore."""
    ultimo = None
    for tentativo in range(3):
        try:
            r = WEB.get(url, timeout=60)
            if r.status_code >= 500:
                raise RuntimeError(f"il sito ha risposto {r.status_code}")
            r.encoding = r.encoding or r.apparent_encoding   # accenti giusti anche senza dichiarazione
            return r.status_code, r.headers.get("Content-Type", ""), r.text
        except Exception as e:      # pagina lenta o errore temporaneo: riprovo
            ultimo = e
            time.sleep(3 + 3 * tentativo)
    raise RuntimeError(f"pagina non raggiungibile ({ultimo})")


def salva_pagina(url, html, estensione=".html"):
    """Salva una copia della pagina in pagine_salvate/ (finisce negli Artifacts del lancio).
    Il nome del file è ricavato dall'indirizzo, es. 49er.org_events_2025-world-championship.html.
    Un nome che inizia con "json/" finisce nella sottocartella json/."""
    try:
        sotto = ""
        if url.startswith("json/"):
            sotto, url = "json", url[5:]
        cartella = os.path.join(CARTELLA_PAGINE, sotto)
        os.makedirs(cartella, exist_ok=True)
        nome = re.sub(r"[^A-Za-z0-9._-]+", "_", re.sub(r"https?://(www\.)?", "", url)).strip("_")[:150]
        with open(os.path.join(cartella, nome + estensione), "w", encoding="utf-8") as f:
            f.write(html)
    except OSError as e:          # un problema di salvataggio non deve fermare l'agente
        log(f"  (copia della pagina non salvata: {e})")


# ====================================================================== BROWSER AUTOMATICO
class Browser:
    """Un solo Chromium senza finestra per tutto il lancio (aprirlo costa qualche secondo).

    apri(url) restituisce il codice HTML DOPO che il JavaScript della pagina ha lavorato,
    e salva in pagine_salvate/json/ le risposte JSON che la pagina ha scaricato.
    """

    def __init__(self):
        self._pw = self._browser = None
        self.json_visti = []          # (indirizzo, lunghezza) dei dati JSON scaricati dall'ultima pagina

    def disponibile(self):
        return sync_playwright is not None

    def _avvia(self):
        if self._browser is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)

    def apri(self, url, attesa_tabella_ms=20000):
        self._avvia()
        pagina = self._browser.new_page(user_agent=UA, locale="en-GB")
        self.json_visti = []
        catturati = []

        def su_risposta(risposta):
            # annoto solo i dati JSON (spesso è lì che arriva la classifica)
            try:
                if "json" in (risposta.headers.get("content-type") or ""):
                    catturati.append(risposta)
            except Exception:
                pass

        pagina.on("response", su_risposta)
        try:
            pagina.goto(url, wait_until="networkidle", timeout=90000)
        except Exception as e:            # pagina lenta: proseguo con quello che c'è
            log(f"  (il browser non ha finito di caricare: {str(e)[:120]})")
        try:
            pagina.wait_for_selector("table", timeout=attesa_tabella_ms)
        except Exception:
            pass                          # nessuna tabella comparsa: lo dirà la diagnosi
        html = pagina.content()
        for i, r in enumerate(catturati[:40]):
            try:
                corpo = r.text()
            except Exception:
                continue
            self.json_visti.append((r.url, len(corpo)))
            salva_pagina(f"json/{i:02d}_{r.url}", corpo, estensione=".json")
        pagina.close()
        return html

    def chiudi(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


BROWSER = Browser()


# ====================================================================== PULIZIA DEI TESTI
def pulisci(testo):
    """Toglie spazi doppi e a capo superflui."""
    return re.sub(r"\s+", " ", testo or "").strip()


def chiave_titolo(testo):
    """Titolo di una colonna ridotto all'osso: 'Sail No.' -> 'sailno', 'Nett Pts' -> 'nettpts'."""
    t = unicodedata.normalize("NFKD", testo or "").lower()
    return re.sub(r"[^a-z0-9#]", "", t)


def testo_cella(cella):
    """Testo di una cella. Gli a capo (<br>, <p>, <div>) diventano "\n": servono a separare
    timoniere e prodiere quando stanno nella stessa cella. Se la cella contiene solo una
    bandierina, uso il testo alternativo dell'immagine (spesso è il codice della nazione).
    Tolgo le parti nascoste (Sailti vi mette codici per l'ordinamento, es. "a_0000001") e il
    circolo velico (Sailti lo scrive tra un nome e l'altro dell'equipaggio)."""
    for nascosto in cella.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
        nascosto.decompose()
    for circolo in cella.find_all(class_=re.compile(r"club", re.I)):
        circolo.decompose()
    for br in cella.find_all("br"):
        br.replace_with("\n")
    for blocco in cella.find_all(["p", "div", "li"]):
        blocco.insert_after("\n")
    righe = [pulisci(r) for r in cella.get_text("").split("\n")]
    testo = "\n".join(r for r in righe if r)
    if not testo:
        img = cella.find("img")
        if img is not None:
            testo = pulisci(img.get("alt") or img.get("title") or "")
    return testo


# ====================================================================== RICONOSCIMENTO DELLE COLONNE
# Per ogni dato, le forme in cui compare il titolo della colonna nelle varie piattaforme.
# Il confronto si fa sulla "chiave" del titolo (minuscole, senza spazi né punteggiatura).
TITOLI = {
    "posizione": re.compile(r"^(rank|rk|pos|position|place|plc|pl|overall|ranking|#)$"),
    "velico": re.compile(r"^(sail|sail#|sailno|sailnumber|sailnum|sailnr|boat|boatno|bow|bow#|bowno|nrvelico|numerovelico)$"),
    "nazione": re.compile(r"^(nat|nation|nationality|country|noc|ioc|mna|flag|naz|nazione|ctry)$"),
    "timoniere": re.compile(r"^(helm|helmname|helmsman|skipper|name|names|sailor|sailors|competitor|"
                            r"competitors|athlete|athletes|team|crewnames|timoniere|nome|rider|riders)$"),
    "prodiere": re.compile(r"^(crew|crewname|crew1|prodiere|crewmember)$"),
    "idws": re.compile(r"^(wsid|sailorid|worldsailingid|isafid|wsno|memberid|ws)$"),
    "netti": re.compile(r"^(net|nett|netto|netpts|nettpts|netpoints|nettpoints|netscore|nettscore|nettotal|"
                        r"total(net|nett)|punti|points|pts)$"),
    "totale": re.compile(r"^(total|tot|totalpts|totalpoints|gross)$"),
}
# Colonne delle singole prove: R1, Q3, F2, M, MR, Race 4, QF, SF, Final...
PROVA = re.compile(r"^(r|q|f|m|mr|race|el|e|qs|fs|fl|g|s)?\d{1,2}$|^(m|mr|medal|medalrace|qf|sf|final|fin)$|"
                   r"^\d{1,2}[a-z]{1,4}$")      # Sailti: "1P", "2P", "1 (US)"
# Sigle delle prove non partite (per riconoscere le barche "mai partite": foglio, punto 13)
NON_PARTITA = re.compile(r"\b(DNC|DNS)\b")


def riconosci_colonne(titoli):
    """Da un elenco di titoli di colonna ricava {dato: indice della colonna}.

    Esempio: ["Rank", "Sail No", "Helm", "Crew", "Nat", "R1", "R2", "Net"] ->
             {"posizione": 0, "velico": 1, "timoniere": 2, "prodiere": 3, "nazione": 4,
              "netti": 7, "prove": [5, 6]}
    """
    mappa, prove = {}, []
    for i, t in enumerate(titoli):
        k = chiave_titolo(t)
        if not k:
            continue
        trovato = False
        for dato, regola in TITOLI.items():
            if dato not in mappa and regola.match(k):
                mappa[dato] = i
                trovato = True
                break
        if not trovato and PROVA.match(k):
            prove.append(i)
    # se c'è solo "Total" (senza "Net"), il totale fa da punteggio
    if "netti" not in mappa and "totale" in mappa:
        mappa["netti"] = mappa["totale"]
    # Sailti: una sola colonna "CREW" con tutti i nomi (a capo) e nessuna colonna "Helm":
    # quella colonna fa da nome della barca. Solo se c'è un punteggio: così gli elenchi
    # degli iscritti (es. "#, Sail Number, Skipper name, Crew name") non sembrano classifiche.
    if "timoniere" not in mappa and "prodiere" in mappa and "netti" in mappa:
        mappa["timoniere"] = mappa.pop("prodiere")
    mappa["prove"] = prove
    return mappa


def leggi_tabella(tabella):
    """Trasforma una <table> HTML in (titoli, righe), espandendo i colspan."""
    righe_html = tabella.find_all("tr")
    if not righe_html:
        return [], []
    # riga dei titoli: l'ultima riga del <thead> se c'è, altrimenti la prima riga con celle <th>,
    # altrimenti la prima riga
    intestazione = None
    thead = tabella.find("thead")
    if thead and thead.find_all("tr"):
        intestazione = thead.find_all("tr")[-1]
    if intestazione is None:
        intestazione = next((tr for tr in righe_html if tr.find("th")), righe_html[0])

    def celle(tr):
        out = []
        for c in tr.find_all(["td", "th"], recursive=False) or tr.find_all(["td", "th"]):
            try:
                span = max(1, min(int(c.get("colspan", 1)), 20))
            except ValueError:
                span = 1
            out.extend([testo_cella(c)] + [""] * (span - 1))
        return out

    titoli = [pulisci(t.replace("\n", " ")) for t in celle(intestazione)]
    corpo, ws = [], []
    dopo = False
    for tr in righe_html:
        if tr is intestazione:
            dopo = True
            continue
        if not dopo or tr.find_parent("thead") is not None:
            continue
        # ID World Sailing: alcune piattaforme (Sailti) collegano ogni nome al profilo
        # https://www.sailing.org/sailor?ref=ESPJX1  → ESPJX1
        ids = re.findall(r"sailing\.org/sailors?[^\"'<>]*?[?&]ref=([A-Za-z0-9]+)", str(tr))
        valori = celle(tr)
        if any(valori):
            corpo.append(valori)
            ws.append(list(dict.fromkeys(ids)))
    return titoli, corpo, ws


def numero(testo):
    """Primo numero di un testo: '12th' -> 12, '=3' -> 3, '45.0' -> 45.0, '(23)' -> 23."""
    m = re.search(r"-?\d+(?:[.,]\d+)?", testo or "")
    if not m:
        return None
    v = float(m.group(0).replace(",", "."))
    return int(v) if v.is_integer() else v


def codice_nazione(*testi):
    """Codice di 3 lettere maiuscole dalla colonna nazione o dal numero velico ('ITA 213' -> ITA)."""
    for t in testi:
        m = re.search(r"\b([A-Z]{3})\b", t or "")
        if m:
            return m.group(1)
    return ""


def dividi_equipaggio(testo):
    """'Ruggero TITA / Caterina BANTI' -> ('Ruggero TITA', 'Caterina BANTI').

    Separatori riconosciuti: a capo, ' / ', ' & ', ' and ', ' - ' tra due nomi, ' + '.
    """
    parti = [p for p in (testo or "").split("\n") if p.strip()]
    if len(parti) >= 2:
        return pulisci(parti[0]), pulisci(parti[1])
    for sep in (r"\s/\s", r"\s&\s", r"\sand\s", r"\s\+\s", r"\s[-–]\s"):
        pezzi = re.split(sep, testo or "", maxsplit=1)
        if len(pezzi) == 2 and all(p.strip() for p in pezzi):
            return pulisci(pezzi[0]), pulisci(pezzi[1])
    return pulisci(testo), ""


def senza_nazione(nome, noc):
    """Toglie da un nome il codice della nazione attaccato davanti o dietro
    ('ITA Marta MAGGETTI' o 'Marta MAGGETTI (ITA)' -> 'Marta MAGGETTI').
    Tolgo solo il codice della nazione della barca: un cognome di tre lettere come KIM o LEE resta."""
    nome = pulisci(nome)
    if not noc:
        return nome
    nome = re.sub(rf"^\(?{noc}\)?\s+", "", nome)
    nome = re.sub(rf"\s+\(?{noc}\)?$", "", nome)
    return pulisci(nome)


TUTTE_LE_TABELLE = []   # (indice, titoli, righe) di ogni tabella della pagina, per la diagnosi


def tabelle_candidate(html):
    """Tutte le tabelle della pagina che hanno almeno una colonna posizione e una colonna nome.

    Restituisce una lista di dizionari {indice, titoli, righe, mappa}.
    """
    zuppa = BeautifulSoup(html, "lxml")
    candidate, tutte = [], zuppa.find_all("table")
    TUTTE_LE_TABELLE.clear()
    for i, t in enumerate(tutte):
        if t.find("table"):        # tabella che contiene altre tabelle: uso quelle interne
            TUTTE_LE_TABELLE.append((i, "contiene altre tabelle", 0))
            continue
        titoli, righe, ws = leggi_tabella(t)
        TUTTE_LE_TABELLE.append((i, titoli[:10], len(righe)))
        mappa = riconosci_colonne(titoli)
        # posizione in una prima colonna senza titolo (Sailti, Mondiali iQFOiL): la accetto se
        # quasi tutte le righe hanno lì un numero
        if "posizione" not in mappa and titoli and not titoli[0].strip() and righe:
            numeri = sum(1 for r in righe if r and re.fullmatch(r"=?\d{1,3}\D{0,2}", r[0].strip()))
            if numeri >= 0.8 * len(righe):
                mappa["posizione"] = 0
        if "posizione" in mappa and "timoniere" in mappa and righe:
            # etichetta: didascalia della tabella o titolo più vicino sopra di essa (es. "ILCA 7 Men"):
            # serve a capire, nella diagnosi, di quale classe è la tabella
            vicino = t.find("caption") or t.find_previous(["h1", "h2", "h3", "h4", "h5", "caption"])
            etichetta = pulisci(vicino.get_text(" "))[:80] if vicino else ""
            candidate.append({"indice": i, "titoli": titoli, "righe": righe, "mappa": mappa,
                              "etichetta": etichetta, "ws": ws})
    return candidate, len(tutte)


def scegli_tabelle(candidate, indice_scelto=None):
    """Sceglie quali tabelle formano la classifica.

    - se eventi_vela.csv indica il numero della tabella (colonna "tabella"), uso quella;
    - se ci sono più tabelle con posizioni che NON si sovrappongono (es. Gold 1-50 e Silver 51-98),
      le unisco: è la classifica finale divisa per flotte;
    - altrimenti prendo la tabella con più righe.
    """
    if not candidate:
        return []
    if indice_scelto is not None:
        return [c for c in candidate if c["indice"] == indice_scelto]

    def intervallo(c):
        pos = [numero(r[c["mappa"]["posizione"]]) for r in c["righe"]
               if c["mappa"]["posizione"] < len(r)]
        pos = [p for p in pos if isinstance(p, int)]
        return (min(pos), max(pos)) if pos else (0, -1)

    ordinate = sorted(candidate, key=lambda c: intervallo(c)[0])
    unite, fine = [], 0
    for c in ordinate:
        a, b = intervallo(c)
        if b < a:
            continue
        if a > fine:           # comincia dopo la tabella precedente: è la flotta successiva
            unite.append(c)
            fine = b
    if len(unite) > 1 and intervallo(unite[0])[0] == 1:
        return unite
    return [max(candidate, key=lambda c: len(c["righe"]))]


# Parole con cui una classe compare nel titolo di una tabella (per suggerire la tabella giusta
# quando una pagina contiene più classi). Il genere si controlla a parte.
PAROLE_CLASSE = {
    "Men's Laser ILCA 7": r"ilca[\s_-]*7|\blaser\b(?!.*radial)",
    "Women's Laser Radial ILCA 6": r"ilca[\s_-]*6|radial",
    "Men's iQFOIL": r"iq[\s_-]*foil", "Women's iQFOIL": r"iq[\s_-]*foil",
    "Men's 49er": r"49er(?![\s_-]*fx)", "Women's 49er FX": r"49er[\s_-]*fx",
    "Mixed 470": r"(?<!\d)470(?!\d)", "Nacra 17": r"nacra|\bn17\b",
    "Men's Kite": r"kite", "Women's Kite": r"kite",
}
DONNE = re.compile(r"women|woman|female|ladies|femm|\bw\b", re.I)


def corrisponde_classe(testo, classe):
    """Vero se un'etichetta (es. 'ilca-6', 'IQ FOIL Women', '49erFX') indica quella classe."""
    regola = re.compile(PAROLE_CLASSE[classe], re.I)
    serve_genere = classe in {"Men's iQFOIL", "Women's iQFOIL", "Men's Kite", "Women's Kite"}
    donna = classe.startswith("Women")
    return bool(regola.search(testo or "")) and (not serve_genere or bool(DONNE.search(testo)) == donna)


def tabella_suggerita(candidate, classe):
    """Indice della tabella il cui titolo corrisponde alla classe (None se nessuna o più d'una)."""
    buone = [c["indice"] for c in candidate if corrisponde_classe(c["etichetta"], classe)]
    return buone[0] if len(buone) == 1 else None


def righe_classifica(tabelle, classe):
    """Trasforma le tabelle scelte nelle barche della classifica (una per riga)."""
    barche = []
    for t in tabelle:
        m = t["mappa"]

        def prendi(riga, dato):
            i = m.get(dato)
            return riga[i] if isinstance(i, int) and i < len(riga) else ""

        for n_riga, riga in enumerate(t["righe"]):
            pos = numero(prendi(riga, "posizione"))
            nome = prendi(riga, "timoniere")
            if not isinstance(pos, int) or not nome.strip():
                continue          # riga di separazione, titolo di flotta, ecc.
            velico = pulisci(prendi(riga, "velico"))
            nazione = codice_nazione(prendi(riga, "nazione"), velico)
            if classe in DOPPI:
                if "prodiere" in m:
                    timoniere, prodiere = pulisci(nome), pulisci(prendi(riga, "prodiere"))
                else:
                    timoniere, prodiere = dividi_equipaggio(nome)
            else:
                timoniere, prodiere = pulisci(nome.replace("\n", " ")), ""
            timoniere, prodiere = senza_nazione(timoniere, nazione), senza_nazione(prodiere, nazione)
            # ID World Sailing: se la piattaforma li mostra (per un equipaggio possono essere due)
            ids = re.findall(r"[A-Z]{3}[A-Z]{0,4}\d{1,6}|\d{4,8}", prendi(riga, "idws"))
            if not ids and t.get("ws") and n_riga < len(t["ws"]):
                ids = t["ws"][n_riga]          # ID presi dai link ai profili World Sailing
            # barca "mai partita": tutte le prove con DNC o DNS (foglio, punto 13)
            prove = [riga[i] for i in m["prove"] if i < len(riga) and riga[i].strip()]
            mai_partita = bool(prove) and all(NON_PARTITA.search(p.upper()) for p in prove)
            barche.append({
                "posizione": pos, "timoniere": timoniere, "prodiere": prodiere,
                "noc": nazione, "velico": velico,
                "id1": ids[0] if ids else "", "id2": ids[1] if len(ids) > 1 else "",
                "punti": numero(prendi(riga, "netti")),
                "note": "mai partita (DNC/DNS in tutte le prove)" if mai_partita else "",
            })
    barche.sort(key=lambda b: b["posizione"])
    return barche


# ====================================================================== ELENCO DEGLI EVENTI
def leggi_eventi():
    """Legge eventi_vela.csv (separatore punto e virgola, si apre con Excel).

    Colonne: id_evento;nome;tipo;data_inizio;data_fine;classe;url;tabella;note
    Le righe che iniziano con # sono commenti.
    """
    if not os.path.exists(FILE_EVENTI):
        die(f"manca il file {os.path.basename(FILE_EVENTI)}")
    with open(FILE_EVENTI, encoding="utf-8-sig") as f:
        righe = [r for r in f if r.strip() and not r.lstrip().startswith("#")]
    lettore = csv.DictReader(righe, delimiter=";")
    eventi, problemi = [], []
    for n, r in enumerate(lettore, start=2):
        e = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if SOLO_EVENTI and e.get("id_evento") not in SOLO_EVENTI:
            continue
        guai = []
        if not e.get("id_evento"):
            guai.append("manca id_evento")
        if e.get("tipo") not in TIPI_EVENTO:
            guai.append(f"tipo '{e.get('tipo')}' non previsto ({', '.join(sorted(TIPI_EVENTO))})")
        if e.get("classe") not in CLASSI:
            guai.append(f"classe '{e.get('classe')}' non prevista")
        for campo in ("data_inizio", "data_fine"):
            if e.get(campo) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", e[campo]):
                guai.append(f"{campo} '{e[campo]}' non è nel formato AAAA-MM-GG")
        # senza data la pagina viene letta lo stesso (diagnosi), ma non scritta in Notion:
        # la data serve all'ELO per mettere le regate in ordine
        e["_senza_data"] = bool(e.get("url")) and not e.get("data_inizio")
        if e.get("tabella") and not e["tabella"].isdigit():
            guai.append("tabella deve essere un numero (o vuota)")
        e["_riga"], e["_guai"] = n, guai
        if guai:
            problemi.append(f"riga {n} ({e.get('id_evento') or '?'} {e.get('classe') or ''}): " + "; ".join(guai))
        eventi.append(e)
    return eventi, problemi


# ====================================================================== MODALITÀ "SCOPRI"
FONTI_SCOPERTA = ["https://www.sailing.org/events/", "https://www.sailinggrandslam.com/"]
# piattaforme di risultati conosciute e parole che indicano una pagina di risultati
PIATTAFORME = re.compile(r"manage2sail|sailti|sailwave|fir\.com\.pl|regattanetwork|yachtscoring|"
                         r"racingrulesofsailing|regatta-results|sailingresults|clubspot|"
                         r"49er\.org|ilcasailing|470\.org|iqfoil|formulakite|kieler-woche|"
                         r"dutchwaterweek|semaine-olympique|trofeoprincesasofia", re.I)
PAROLE_RISULTATI = re.compile(r"result|standing|classifica|resultat|ergebnis|uitslag|clasificaci|leaderboard", re.I)
MAX_PAGINE_SCOPERTA = 150


def scopri():
    """Esplora le due fonti indicate da Gualtiero e raccoglie i link ai risultati."""
    trovati, visitate = [], set()
    for fonte in FONTI_SCOPERTA:
        dominio = urlparse(fonte).netloc.replace("www.", "")
        log(f"\n=== Esploro {fonte}")
        try:
            codice, tipo, html = scarica(fonte)
        except RuntimeError as e:
            log(f"  NON raggiungibile: {e}")
            continue
        log(f"  risposta {codice} · {tipo} · {len(html)} caratteri")
        salva_pagina(fonte, html)
        if codice >= 400:
            log("  il sito rifiuta la richiesta (blocco anti-robot?). Il blocco non va aggirato: "
                "in quel caso si compila eventi_vela.csv a mano.")
            continue
        usa_browser = False
        if len(re.findall(r"<a\s", html)) < 40 and BROWSER.disponibile():
            # pochi link: l'elenco degli eventi lo costruisce il JavaScript (sailing.org usa Nuxt).
            log("  pochi link nella pagina scaricata: la riapro con il browser automatico...")
            try:
                html = BROWSER.apri(fonte, attesa_tabella_ms=3000)
                salva_pagina(fonte + "__browser", html)
                usa_browser = True
                n_link = len(re.findall(r"<a\s", html))
                log(f"  con il browser: {n_link} link")
            except Exception as err:
                log(f"  il browser non è riuscito ad aprire la pagina: {str(err)[:200]}")
        zuppa = BeautifulSoup(html, "lxml")
        # campione dei link interni della pagina: serve a capire come sono fatti gli indirizzi degli eventi
        interni = []
        for a in zuppa.find_all("a", href=True):
            u = urljoin(fonte, a["href"]).split("#")[0]
            if dominio in urlparse(u).netloc and u not in interni:
                interni.append(u)
        log(f"  link interni nella pagina: {len(interni)}; primi 30:")
        for u in interni[:30]:
            log(f"    {u}")
        da_visitare = []
        for a in zuppa.find_all("a", href=True):
            url = urljoin(fonte, a["href"]).split("#")[0]
            testo = pulisci(a.get_text(" "))
            if PIATTAFORME.search(url) or PAROLE_RISULTATI.search(testo + " " + url):
                trovati.append((fonte, fonte, testo, url))
            # pagine interne da esplorare: eventi (sailing.org) o tutte le pagine interne (SGS)
            interna = dominio in urlparse(url).netloc
            if interna and url not in visitate and url.rstrip("/") != fonte.rstrip("/"):
                # su sailing.org esploro solo le pagine che sembrano eventi
                if "sailing.org" in dominio and not re.search(
                        r"/events?/|championship|regatta|world-cup|grand-slam|olympic", url, re.I):
                    continue
                da_visitare.append(url)
        log(f"  link interni da esplorare: {len(set(da_visitare))}")
        if len(re.findall(r"<a\s", html)) < 15:
            log("  ATTENZIONE: la pagina ha pochissimi link anche così: l'elenco degli eventi non è leggibile.")
        for url in list(dict.fromkeys(da_visitare))[:MAX_PAGINE_SCOPERTA]:
            if url in visitate:
                continue
            visitate.add(url)
            time.sleep(PAUSA_WEB)
            try:
                if usa_browser:            # stesso sito costruito con JavaScript: serve il browser
                    codice, tipo, html = 200, "text/html", BROWSER.apri(url, attesa_tabella_ms=2000)
                else:
                    codice, tipo, html = scarica(url)
            except Exception as e:
                log(f"  - {url}: {str(e)[:160]}")
                continue
            if codice >= 400 or "html" not in tipo.lower():
                continue
            pagina = BeautifulSoup(html, "lxml")
            titolo = pulisci(pagina.title.get_text()) if pagina.title else ""
            n = 0
            for a in pagina.find_all("a", href=True):
                link = urljoin(url, a["href"]).split("#")[0]
                testo = pulisci(a.get_text(" "))
                esterno = dominio not in urlparse(link).netloc
                if PIATTAFORME.search(link) or (PAROLE_RISULTATI.search(testo + " " + link) and esterno):
                    trovati.append((fonte, url, f"{titolo} | {testo}", link))
                    n += 1
            if n:
                log(f"  + {titolo[:70]}: {n} link di risultati")

    # tolgo i doppioni e salvo il file per Gualtiero
    visti, unici = set(), []
    for t in trovati:
        if t[3] not in visti:
            visti.add(t[3])
            unici.append(t)
    with open(FILE_TROVATI, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["fonte", "pagina evento", "titolo | testo del link", "link risultati", "piattaforma"])
        for fonte, pagina, testo, link in unici:
            m = PIATTAFORME.search(link)
            w.writerow([fonte, pagina, testo, link, m.group(0).lower() if m else ""])
    log(f"\nLink di risultati trovati: {len(unici)} (file {os.path.basename(FILE_TROVATI)} negli Artifacts).")
    piattaforme = Counter((PIATTAFORME.search(t[3]).group(0).lower() if PIATTAFORME.search(t[3]) else "altro")
                          for t in unici)
    log("Per piattaforma: " + (", ".join(f"{k} {v}" for k, v in piattaforme.most_common()) or "nessuna"))
    log("Nulla è stato scritto su Notion (la modalità 'scopri' non scrive mai).")


# ====================================================================== NOTION
def notion(metodo, percorso, **kwargs):
    """Chiamata all'API Notion; aspetta e riprova se Notion è occupato (429) o in errore (5xx)."""
    r = None
    for tentativo in range(5):
        r = requests.request(metodo, "https://api.notion.com/v1" + percorso,
                             headers=NOTION_HEADERS, timeout=30, **kwargs)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("Retry-After", 2)) + tentativo)
            continue
        break
    if r.status_code == 404:
        raise RuntimeError("Notion non trova il database: aprilo, ••• > Connessioni, "
                           "aggiungi la connessione \"Agente Squash\".")
    if not r.ok:
        raise RuntimeError(f"Notion ha risposto {r.status_code}: {r.text[:400]}")
    return r.json()


def leggi_schema():
    """Legge le colonne del db e le associa ai dati. Si ferma se manca una colonna obbligatoria."""
    props = notion("GET", f"/databases/{DATABASE_ID}")["properties"]
    titolo = next(((n, "title") for n, p in props.items() if p["type"] == "title"), None)
    per_nome = {n.strip().lower(): (n, p["type"]) for n, p in props.items() if p["type"] != "title"}
    mappa, mancanti = {"title": titolo}, []
    for chiave, nomi in COLONNE.items():
        trovato = next((per_nome[x.lower()] for x in nomi if x.lower() in per_nome), None)
        if trovato:
            mappa[chiave] = trovato
        elif chiave in FACOLTATIVE:
            log(f"Nota: nel database non c'è la colonna '{nomi[0]}': quel dato non verrà scritto.")
        else:
            mancanti.append(nomi[0])
    if mancanti:
        die("nel database non trovo queste colonne: " + ", ".join(mancanti)
            + ". Colonne presenti: " + ", ".join(props.keys()))
    log("Colonne trovate in Notion: titolo '" + titolo[0] + "' + "
        + ", ".join(f"{n} ({t})" for k, (n, t) in mappa.items() if k != "title"))
    return mappa


def testo_prop(prop):
    parti = prop.get("rich_text") or prop.get("title") or []
    return "".join(p.get("plain_text", "") for p in parti).strip()


def id_esistenti(mappa):
    """Tutti gli "ID Univoco" già presenti nel db (per non scrivere doppioni)."""
    colonna = mappa["id"][0]
    ids, cursore = set(), None
    while True:
        corpo = {"page_size": 100}
        if cursore:
            corpo["start_cursor"] = cursore
        r = notion("POST", f"/databases/{DATABASE_ID}/query", json=corpo)
        for pagina in r["results"]:
            uid = testo_prop(pagina["properties"].get(colonna, {}))
            if uid:
                ids.add(uid)
        if not r.get("has_more"):
            return ids
        cursore = r["next_cursor"]


def valore_notion(tipo, v):
    """Converte un valore nel formato che Notion vuole per quel tipo di colonna."""
    if v is None or v == "":
        return None
    if tipo == "title":
        return {"title": [{"text": {"content": str(v)[:2000]}}]}
    if tipo == "rich_text":
        return {"rich_text": [{"text": {"content": str(v)[:2000]}}]}
    if tipo == "number":
        if isinstance(v, bool):           # attenzione: in Python True vale 1
            return None
        return {"number": float(v)}
    if tipo == "select":
        return {"select": {"name": str(v).replace(",", " ")[:100]}}   # nei select la virgola non è ammessa
    if tipo == "date":
        return {"date": {"start": str(v)}}
    if tipo == "url":
        return {"url": str(v)}
    raise RuntimeError(f"tipo di colonna non gestito: {tipo}. Usa testo, numero, selezione, data o URL.")


def crea_riga(mappa, dati):
    props = {}
    for chiave, (nome, tipo) in mappa.items():
        v = valore_notion(tipo, dati.get(chiave))
        if v is not None:
            props[nome] = v
    notion("POST", "/pages", json={"parent": {"database_id": DATABASE_ID}, "properties": props})


# ====================================================================== MODALITÀ "LEGGI"
def id_univoco(evento, barca, usati):
    """'2026-sgs1-palma|Men's 49er|ITA 213'. Se manca il numero velico uso l'ID World Sailing, poi il nome.
    Se due barche avessero lo stesso identificativo aggiungo #2, #3..."""
    chi = barca["velico"] or barca["id1"] or f"{barca['timoniere']}/{barca['prodiere']}".strip("/")
    base = f"{evento['id_evento']}|{evento['classe']}|{chi}"
    uid, n = base, 1
    while uid in usati:
        n += 1
        uid = f"{base}#{n}"
    usati.add(uid)
    return uid


def diagnosi_classifica(evento, candidate, n_tabelle, scelte, barche):
    """Stampa ciò che serve per capire se la pagina è stata letta bene."""
    log(f"  tabelle nella pagina: {n_tabelle}, con posizione e nome: {len(candidate)}")
    for c in candidate[:8]:
        riconosciute = {k: c["titoli"][v] for k, v in c["mappa"].items() if k != "prove"}
        log(f"    · tabella {c['indice']} «{c['etichetta']}»: {len(c['righe'])} righe · colonne riconosciute {riconosciute}"
            f" · prove {len(c['mappa']['prove'])}")
        log(f"      titoli: {c['titoli'][:14]}")
        # primo classificato: aiuta a capire di quale classe è la tabella
        primo = righe_classifica([c], evento["classe"])[:1]
        if primo:
            b = primo[0]
            log(f"      primo classificato: {b['timoniere']}" + (f" / {b['prodiere']}" if b["prodiere"] else "")
                + f" ({b['noc'] or '?'})")
    indici_candidate = {c["indice"] for c in candidate}
    for i, titoli, n in TUTTE_LE_TABELLE:
        if i not in indici_candidate:
            log(f"    · tabella {i} (non è una classifica): {n} righe · titoli {titoli}")
    if len(candidate) > 8:
        log(f"    ... e altre {len(candidate) - 8} tabelle")
    log(f"  tabelle usate: {[c['indice'] for c in scelte]}"
        + ("  (unite: classifica divisa per flotte)" if len(scelte) > 1 else ""))
    if evento.get("tabella") and not scelte:
        log(f"  ATTENZIONE: la tabella {evento['tabella']} indicata in eventi_vela.csv non è una tabella "
            "di classifica (vedi l'elenco qui sopra).")
    if len(candidate) > len(scelte) and not evento.get("tabella"):
        sugg = tabella_suggerita(candidate, evento["classe"])
        log("  NOTA: la pagina ha altre tabelle di classifica. Se quella usata non è giusta, scrivi il "
            "numero della tabella nella colonna 'tabella' di eventi_vela.csv."
            + (f" Suggerita dal titolo: tabella {sugg}." if sugg is not None else ""))
    if not barche:
        return
    for b in barche[:3] + ([barche[-1]] if len(barche) > 3 else []):
        log(f"    {b['posizione']:>3}. {b['timoniere']}" + (f" / {b['prodiere']}" if b["prodiere"] else "")
            + f" · {b['noc'] or '?'} · velico '{b['velico']}' · punti {b['punti']}"
            + (f" · ID WS {b['id1']}/{b['id2']}" if b["id1"] else "") + (f" · {b['note']}" if b["note"] else ""))
    posizioni = [b["posizione"] for b in barche]
    mancanti = sorted(set(range(1, max(posizioni) + 1)) - set(posizioni))
    doppie = [p for p, n in Counter(posizioni).items() if n > 1]
    avvisi = []
    if mancanti:
        avvisi.append(f"posizioni mancanti {mancanti[:15]}{'...' if len(mancanti) > 15 else ''}")
    if doppie:
        avvisi.append(f"posizioni ripetute {sorted(doppie)[:10]} (pari merito o tabelle doppie?)")
    if evento["classe"] in DOPPI and sum(1 for b in barche if b["prodiere"]) < len(barche) / 2:
        avvisi.append("classe in doppio ma il prodiere manca in più di metà delle righe")
    if not any(b["noc"] for b in barche):
        avvisi.append("nessuna nazione riconosciuta")
    if sum(1 for b in barche if not b["velico"]) > len(barche) / 2:
        avvisi.append("numero velico assente in più di metà delle righe (l'ID Univoco userà il nome)")
    n_id = sum(1 for b in barche if b["id1"])
    log(f"  barche: {len(barche)} · con ID World Sailing: {n_id} · mai partite: "
        f"{sum(1 for b in barche if b['note'])}")
    for a in avvisi:
        log(f"  ATTENZIONE: {a}")


def schede_sailti(url, html):
    """Elenco delle classi di una pagina Sailti "resultsall": [(etichetta, indirizzo resultsajax)].

    Sailti scrive le classi in due modi:
      - menu a tendina (Princesa Sofía): <option data-href="/en/default/races/resultsajax?...">49er</option>
      - schede con icone (Sailing Grand Slam, siti degli eventi): <a id="mnResults125_2261"
        title="IQ FOIL Women"> con dentro <div class="ico-sailclass ilca-6">; l'indirizzo della
        classifica sta nello script subito dopo "$('#mnResults125_2261').parent().addClass('active')".
    """
    zuppa = BeautifulSoup(html, "lxml")
    schede = []
    # 1) menu a tendina
    for opz in zuppa.find_all("option"):
        dh = opz.get("data-href") or ""
        if "resultsajax" in dh:
            schede.append((pulisci(opz.get_text(" ")), urljoin(url, dh)))
    # 2) schede con icone: etichetta = titolo della scheda o nome della classe nell'icona
    etichette = {}
    generiche = {"ico-sailclass", "on", "off", "class-icon", "active"}
    for a in zuppa.find_all("a", id=re.compile(r"^mnResults\d+_\d+$")):
        parole = [a.get("title") or ""]
        parole += [c for c in (a.get("class") or []) if c not in generiche and not c.isdigit()
                   and not c.startswith("race")]
        for d in a.find_all("div"):
            parole += [c for c in (d.get("class") or []) if c not in generiche]
        parole.append(pulisci(a.get_text(" ")))
        etichette[a["id"]] = pulisci(" ".join(p for p in parole if p))
    for m in re.finditer(r"""url:\s*['"]([^'"]*resultsajax[^'"]*)['"]""", html):
        prima = html[max(0, m.start() - 700):m.start()]
        ids = re.findall(r"#(mnResults\d+_\d+)['\"]\)", prima)   # ultima scheda nominata prima dell'indirizzo
        if ids and ids[-1] in etichette:
            schede.append((etichette[ids[-1]], urljoin(url, m.group(1))))
    # tolgo i doppioni (la prima classe compare due volte: caricamento iniziale e clic)
    visti, uniche = set(), []
    for et, u in schede:
        if u not in visti:
            visti.add(u)
            uniche.append((et, u))
    return uniche


def esamina_pagina_js(url, html):
    """Per una pagina senza tabelle elenca gli indizi su dove il browser va a prendere i dati:
    iframe, menu a tendina (spesso l'elenco delle classi), script, indirizzi negli script,
    link con parole come 'result', 'race', 'class'. Servono a Claude per scrivere il lettore apposito."""
    zuppa = BeautifulSoup(html, "lxml")
    for f in zuppa.find_all("iframe")[:10]:
        log(f"    iframe: {urljoin(url, f.get('src') or '')}")
    for sel in zuppa.find_all("select")[:6]:
        opzioni = [(o.get("value", ""), pulisci(o.get_text(" "))) for o in sel.find_all("option")][:25]
        log(f"    menu '{sel.get('name') or sel.get('id') or '?'}': {opzioni}")
    for sc in zuppa.find_all("script", src=True)[:15]:
        log(f"    script: {urljoin(url, sc['src'])}")
    testo_script = " ".join(sc.get_text(" ") for sc in zuppa.find_all("script") if not sc.get("src"))
    indirizzi = set(re.findall(r"[\"'](/[A-Za-z0-9_\-/.?=&%]*?(?:result|race|class|ajax|api|json)[A-Za-z0-9_\-/.?=&%]*)[\"']",
                               testo_script, re.I))
    indirizzi |= set(re.findall(r"https?://[^\s\"'<>]*(?:result|ajax|api|json)[^\s\"'<>]*", testo_script, re.I))
    for ind in sorted(indirizzi)[:25]:
        log(f"    indirizzo negli script: {ind}")
    link = []
    for a in zuppa.find_all("a", href=True):
        h = urljoin(url, a["href"])
        if re.search(r"result|race|class|regat|standing", h + " " + a.get_text(" "), re.I) and h not in link:
            link.append(h)
    for h in link[:40]:
        log(f"    link: {h}")
    for el in zuppa.find_all(attrs={"data-url": True})[:10]:
        log(f"    data-url: {urljoin(url, el['data-url'])}")


def leggi():
    """Legge le classifiche di eventi_vela.csv e (se non è una prova) le scrive in Notion."""
    eventi, problemi = leggi_eventi()
    log(f"Righe in {os.path.basename(FILE_EVENTI)}: {len(eventi)}"
        + (f" (solo gli eventi: {', '.join(sorted(SOLO_EVENTI))})" if SOLO_EVENTI else ""))
    for p in problemi:
        log(f"  PROBLEMA: {p}")
    da_leggere = [e for e in eventi if e.get("url") and not e["_guai"]]
    senza_url = [e for e in eventi if not e.get("url")]
    if senza_url:
        log(f"Righe senza url (saltate): {len(senza_url)} → "
            + ", ".join(f"{e['id_evento']} {e['classe']}" for e in senza_url[:12])
            + ("..." if len(senza_url) > 12 else ""))

    # stato del db (anche in prova, se il token c'è: è solo lettura)
    mappa, esistenti = None, set()
    if NOTION_TOKEN and DATABASE_ID:
        try:
            mappa = leggi_schema()
            esistenti = id_esistenti(mappa)
            log(f"Barche già presenti in Notion: {len(esistenti)}")
        except (SystemExit, RuntimeError) as e:
            if not DRY_RUN:
                raise
            log(f"In prova continuo senza Notion ({e}).")
    elif not DRY_RUN:
        die("mancano NOTION_TOKEN o NOTION_DATABASE_ID (controlla i Secrets su GitHub).")

    # La stessa pagina usata per più classi (es. Princesa Sofía: una pagina con tutte le classi)
    # va letta indicando il numero della tabella, altrimenti ogni classe prenderebbe la stessa
    # tabella. Nel lancio vero queste righe NON vengono scritte finché la tabella non è indicata.
    per_url = Counter(e["url"] for e in da_leggere)
    ambigue = {(e["id_evento"], e["classe"]) for e in da_leggere if per_url[e["url"]] > 1 and not e.get("tabella")}
    if ambigue:
        log(f"Righe con una pagina condivisa da più classi e senza 'tabella': {len(ambigue)} "
            "(per le pagine Sailti la classe si riconosce da sola; per le altre la riga viene letta "
            "per la diagnosi, ma non scritta in Notion finché non indichi la tabella).")

    riepilogo = []          # (evento, classe, esito)
    scritte = errori = 0
    interrotto = False
    cache = {}              # la stessa pagina può servire a più classi: la scarico una volta sola
    for e in da_leggere:
        if not DRY_RUN and minuti() >= TEMPO_MAX_MINUTI:
            interrotto = True
            break
        log(f"\n=== {e['id_evento']} · {e['classe']} · {e['tipo']} · {e.get('data_inizio')}")
        log(f"  {e['url']}")
        try:
            if e["url"] not in cache:
                time.sleep(PAUSA_WEB)
                cache[e["url"]] = scarica(e["url"])
                salva_pagina(e["url"], cache[e["url"]][2])
            codice, tipo, html = cache[e["url"]]
        except RuntimeError as err:
            log(f"  NON raggiungibile: {err}")
            riepilogo.append((e, "non raggiungibile"))
            errori += 1
            continue
        log(f"  risposta {codice} · {tipo.split(';')[0]} · {len(html)} caratteri")
        if codice >= 400:
            riepilogo.append((e, f"rifiutata ({codice})"))
            continue
        if "pdf" in tipo.lower() or e["url"].lower().endswith(".pdf"):
            log("  è un PDF: questa versione legge solo pagine web. [DA DECIDERE] lettore PDF.")
            riepilogo.append((e, "PDF"))
            continue
        schede = schede_sailti(e["url"], html) if "resultsajax" in html else []
        if schede:
            # ---- piattaforma Sailti: scelgo la classe e scarico solo la sua classifica
            log(f"  pagina Sailti con {len(schede)} classi: " + ", ".join(et for et, _ in schede))
            giuste = [(et, u) for et, u in schede if corrisponde_classe(et, e["classe"])]
            if len(giuste) != 1:
                log(f"  ATTENZIONE: la classe {e['classe']} " + ("non c'è" if not giuste else "è ambigua")
                    + " nell'elenco delle classi di questa pagina.")
                riepilogo.append((e, "classe non trovata nella pagina Sailti" if not giuste else "classe ambigua"))
                continue
            et, u = giuste[0]
            log(f"  classe «{et}» → {u}")
            try:
                if u not in cache:
                    time.sleep(PAUSA_WEB)
                    cache[u] = scarica(u)
                    salva_pagina(u, cache[u][2])
                html = cache[u][2]
            except RuntimeError as err:
                log(f"  classifica della classe non raggiungibile: {err}")
                riepilogo.append((e, "non raggiungibile"))
                errori += 1
                continue
            e["_sailti"] = True
        elif "resultsajax" in html:
            log("  pagina Sailti senza elenco delle classi (forse i risultati non sono ancora pubblicati).")
            testo = pulisci(BeautifulSoup(html, "lxml").get_text(" "))
            if re.search(r"not yet uploaded|no results", testo, re.I):
                log("  il sito dice: risultati non ancora pubblicati.")
            riepilogo.append((e, "Sailti senza risultati"))
            continue
        candidate, n_tabelle = tabelle_candidate(html)
        if not candidate and BROWSER.disponibile() and not e.get("_sailti"):
            # pagina senza classifiche: la riapro con il browser automatico (una volta sola per indirizzo)
            chiave = "browser:" + e["url"]
            if chiave not in cache:
                log("  nessuna classifica nella pagina scaricata: la riapro con il browser automatico...")
                try:
                    html_b = BROWSER.apri(e["url"])
                    salva_pagina(e["url"] + "__browser", html_b)
                    cache[chiave] = (html_b, list(BROWSER.json_visti))
                except Exception as err:
                    log(f"  il browser non è riuscito ad aprire la pagina: {str(err)[:200]}")
                    cache[chiave] = ("", [])
            html_b, json_visti = cache[chiave]
            if json_visti:
                log(f"  dati JSON scaricati dalla pagina: {len(json_visti)} (copie in pagine_salvate/json/)")
                for u, n in json_visti[:12]:
                    log(f"    {n:>8} caratteri · {u[:160]}")
            if html_b:
                html = html_b
                candidate, n_tabelle = tabelle_candidate(html)
                log(f"  con il browser: tabelle {n_tabelle}, classifiche {len(candidate)}")
        scelte = scegli_tabelle(candidate, int(e["tabella"]) if e.get("tabella") else None)
        barche = righe_classifica(scelte, e["classe"])
        diagnosi_classifica(e, candidate, n_tabelle, scelte, barche)
        if not barche:
            if re.search(r"not yet uploaded any race results", html, re.I):
                log("  il sito dice: risultati non ancora pubblicati su questa pagina.")
            if n_tabelle == 0 and re.search(r"<script", html, re.I):
                log("  nessuna tabella: la pagina è probabilmente costruita con JavaScript "
                    "(es. Sailti, Manage2Sail). Serve un lettore apposito. Indizi trovati nella pagina:")
                esamina_pagina_js(e["url"], html)
            riepilogo.append((e, "nessuna classifica letta"))
            continue
        riepilogo.append((e, f"{len(barche)} barche"))

        # preparo le righe per Notion
        usati, nuove = set(), []
        for b in barche:
            uid = id_univoco(e, b, usati)
            if uid in esistenti:
                continue
            titolo = f"{b['timoniere']} / {b['prodiere']}" if b["prodiere"] else b["timoniere"]
            nuove.append({**b, "title": titolo, "classe": e["classe"], "barche": len(barche),
                          "evento": e.get("nome") or e["id_evento"], "tipo": e["tipo"],
                          "inizio": e.get("data_inizio"), "fine": e.get("data_fine") or e.get("data_inizio"),
                          "fonte": e["url"], "id": uid})
        log(f"  da scrivere: {len(nuove)} (già presenti: {len(barche) - len(nuove)})")
        if e["_senza_data"]:
            log("  NON scritta: manca data_inizio in eventi_vela.csv.")
            riepilogo[-1] = (e, f"{len(barche)} barche, ma manca data_inizio")
            continue
        if (e["id_evento"], e["classe"]) in ambigue and not e.get("_sailti"):
            log("  NON scritta: la pagina contiene più classi; indica il numero della tabella "
                "giusta nella colonna 'tabella' di eventi_vela.csv (lo vedi qui sopra).")
            riepilogo[-1] = (e, f"{len(barche)} barche, ma manca 'tabella'")
            continue
        if DRY_RUN or not mappa:
            continue
        for r in nuove:
            if minuti() >= TEMPO_MAX_MINUTI:
                interrotto = True
                break
            try:
                crea_riga(mappa, r)
                esistenti.add(r["id"])
                scritte += 1
                time.sleep(PAUSA_NOTION)
            except RuntimeError as err:
                log(f"  Errore su {r['title']}: {err}")
                errori += 1
        if interrotto:
            break

    # riepilogo finale
    log("\n" + "=" * 78)
    log("RIEPILOGO")
    log("=" * 78)
    for e, esito in riepilogo:
        log(f"  {e['id_evento']:<34} {e['classe']:<28} {esito}")
    esiti = Counter("letta" if r[1].endswith("barche") else r[1] for r in riepilogo)
    log("Pagine: " + ", ".join(f"{k} {v}" for k, v in esiti.most_common()))
    if DRY_RUN:
        log("\nProva terminata: non è stato scritto nulla su Notion.")
    else:
        log(f"Righe scritte: {scritte} · errori: {errori} · minuti: {minuti():.0f}")
    if interrotto:
        log(f"ATTENZIONE: mi sono fermato dopo {TEMPO_MAX_MINUTI:.0f} minuti. Rilancia con le stesse "
            "impostazioni: le barche già scritte vengono saltate.")
        return 1
    return 1 if (errori and not DRY_RUN) else 0


# ====================================================================== MAIN
def main():
    log(f"Agente Vela 1.3 · modalità {MODALITA} · browser automatico "
        f"{'disponibile' if BROWSER.disponibile() else 'NON disponibile (manca playwright)'}")
    log(f"Agente Vela · modalità {MODALITA} · "
        f"{'SOLO PROVA: non scrivo nulla su Notion' if DRY_RUN or MODALITA == 'scopri' else 'SCRITTURA su Notion'} · "
        f"{dt.datetime.now():%Y-%m-%d %H:%M}")
    if MODALITA == "scopri":
        scopri()
        return 0
    if MODALITA == "leggi":
        return leggi()
    die(f"MODALITA deve essere 'scopri' o 'leggi' (hai scritto '{MODALITA}').")


if __name__ == "__main__":
    codice = 1
    try:
        codice = main() or 0
    except SystemExit as e:
        codice = e.code if isinstance(e.code, int) else 1
    except Exception:
        log(traceback.format_exc())
    finally:
        BROWSER.chiudi()
        with open(FILE_RAPPORTO, "w", encoding="utf-8") as f:
            f.write("\n".join(_righe_log) + "\n")
    sys.exit(codice)
