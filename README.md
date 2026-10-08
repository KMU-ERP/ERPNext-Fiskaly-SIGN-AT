# 🇦🇹 ERPNext Fiskaly SIGN-AT Integration

> ⚠️ **BETA-VERSION**
> Diese App befindet sich derzeit in einer aktiven Entwicklungs- und Beta-Phase. Funktionen können sich ohne Vorankündigung ändern, und es können unerwartete Fehler auftreten.

**Die professionelle, ausfallsichere RKSV-Lösung für deine ERPNext Registrierkasse.**

Mit dieser App integrierst du die österreichische Registrierkassensicherheitsverordnung (RKSV) nahtlos in dein ERPNext-System (Frappe 16). Sie verknüpft die POS-Funktionen von ERPNext mit dem cloudbasierten Signatur-Service von **fiskaly** – komplett unsichtbar für den Kassierer im Alltag, aber zu 100 % konform mit den Anforderungen des Finanzamts (inkl. automatischer FinanzOnline-Meldungen).

---

## 🚀 So funktioniert's (Voraussetzungen)

Diese App ist **Open Source** und kann kostenlos in jeder ERPNext 16 Instanz installiert werden. 

Damit die Fiskalisierung technisch funktioniert, benötigt die App im Hintergrund einen Zugang zur **fiskaly Cloud-Infrastruktur** (für die digitale Signatur und Zertifikate). 
Da fiskaly seine Dienste in der Regel nicht direkt an einzelne Endkunden richtet, sondern über Systempartner anbietet, läuft die Einrichtung in zwei einfachen Schritten:

1. **App installieren:** Lade diese App in deine ERPNext-Instanz (siehe [Installation](#installation)).
2. **Fiskaly-Account beziehen:** Du benötigst API-Keys (Test & Live). Als offizieller Fiskaly-Partner übernehmen wir das gerne für dich! 
   👉 **[Kontaktiere uns per E-Mail](mailto:anfrage@kmu-erp.at)**

Sobald du die Keys von uns erhalten hast, trägst du sie in ERPNext ein und deine Kasse ist rechtssicher.

---

## ✨ Hauptfunktionen & Vorteile

* 🛡️ **100% RKSV-Konform:** Erfüllt alle gesetzlichen Anforderungen in Österreich inkl. Signatur-QR-Code auf dem Kassenbon.
* 🔌 **Nahtlose POS-Integration:** Perfekt in die ERPNext POS-Oberfläche integriert. Unterstützt komplexe Szenarien wie Mischzahlungen (Bar & Karte centgenau aufgeteilt).
* 📴 **Ausfallsicher (Offline-Modus):** Fällt das Internet oder die API aus, wird automatisch ein rechtsgültiger Notbeleg ("Sicherheitseinrichtung ausgefallen") gedruckt. Sobald die Verbindung wieder steht, synchronisiert die App alles im Hintergrund (FIFO-Replay).
* 📊 **Automatisches Meldewesen:** Start-, Monats- und Jahresbelege sowie Außerbetriebnahmen werden sauber gehandhabt. Der Jahresbeleg enthält alle erforderlichen Prüf-, Ausdruck- und Archivnachweise.
* 📁 **Einfacher DEP7-Export:** Die vorgeschriebene Datenerfassungsprotokollierung (DEP7) kann jederzeit für beliebige Zeiträume als manipulationssichere Datei exportiert werden.
* 🔒 **Sicherheit geht vor:** Strenge Serialisierung der Belege per Datenbanksperre, Wasserzeichen auf PDF-Duplikaten und manipulationssicheres Lifecycle-Protokoll.

---

## 🛠️ Installation

Wechsle in dein Frappe-Bench-Verzeichnis und führe folgende Befehle aus:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch main
bench install-app erpnext_fiskaly_sign_at
bench --site $SITE migrate

```

---

## ⚙️ Einrichtung in ERPNext

Die App bringt einen eigenen Workspace namens **Fiskaly RKSV** mit, der dich durch den Prozess führt:

1. **API-Keys hinterlegen:** Unter *API-Verbindungen* die von uns erhaltenen Keys für TEST und LIVE eintragen (Secrets werden verschlüsselt gespeichert).
2. **Kasse definieren:** Unter *Registrierkassen / FON* das Unternehmen, POS-Profil und USt-Mapping festlegen.
3. **Provisionieren:** Mit einem Klick das System bei FinanzOnline authentifizieren, die Kasse initialisieren und den Startbeleg erzeugen.
4. **Testen & Aktivieren:** In den *Fiskaly Settings* nach einem erfolgreichen Sandbox-Test auf die LIVE-Umgebung umschalten.
5. **Druckformat prüfen:** Im POS-Profil das RKSV-Druckformat aktivieren, damit der QR-Code sauber auf den Bons landet.

*(Eine detaillierte Schritt-für-Schritt-Anleitung mit allen rechtlichen Rahmenbedingungen findest du direkt in ERPNext im Workspace "Fiskaly RKSV").*

---

## 🔬 Unter der Haube (Für Administratoren & Entwickler)

* **Architektur & Versionen:** Die App kapselt fiskaly hinter einem stabilen Provider-Vertrag. Derzeit sind die Adapter `sign_at_v1.py` (Produktiv) und `sign_at_unified.py` (Unified 2026-06-01; aktuell nur TEST) enthalten.
* **Transaktionssicherheit:** Pro Kasse werden Belege mit einer Datenbanksperre strikt serialisiert. Outbox und idempotenter Signaturversuch erfolgen im `Submit`-Vorgang, *bevor* der Beleg gedruckt werden kann.
* **Fehlerbehandlung (Fail-Closed):** Permanente Payload-/Konfigurationsfehler erzeugen *keinen* falschen Ausfallbon; der POS-Submit wird sicherheitshalber zurückgewiesen.
* **Storno & Duplikate:** Direkte Stornos von Fiskalbelegen sind blockiert (nur via ERPNext-Rückgabebeleg möglich). Direkte Core-PDF-Druckwege sind gesperrt; die Desk-Vorschau trägt ein Wasserzeichen ("DUPLIKAT"), um versehentliche Doppel-Originale zu verhindern.
* **Steuer-Mapping:** Vor dem Submit wird das VAT-Mapping strikt lokal validiert. Artikel-Netto, Umsatzsteuer und Grand Total müssen centgenau übereinstimmen. `0 → zero` ist streng auf steuerfreie Umsätze limitiert.

---

## 🧪 Tests vor dem Produktivbetrieb

Bevor ihr in den Echtbetrieb startet, sollten alle Szenarien in der TEST-Umgebung mit dem steuerlichen Verantwortlichen durchgespielt werden:

```bash
bench --site $SITE set-config allow_tests true
bench --site $SITE run-tests --app erpnext_fiskaly_sign_at

```

*Tipp:* Testet Mischzahlungen, API-Ausfälle (Offline-Modus), Retouren und den DEP7-Export!

---

## 💻 Entwicklung & Contribution

Wir nutzen `pre-commit` für Code Formatting und Linting (ruff, eslint, prettier, pyupgrade). Bitte installiere es vor deinem ersten Pull Request:

```bash
cd apps/erpnext_fiskaly_sign_at
pre-commit install

```

*(Das projektspezifische Desktop-Symbol liegt unter `public/images/kmu_erp_logo.svg`.)*

---

## 📜 Lizenz & Haftungsausschluss

Diese Software wird unter der **GPLv3** Lizenz bereitgestellt.

*Hinweis: Diese Software erleichtert die technische Umsetzung der RKSV, ersetzt jedoch keine rechtliche oder steuerliche Beratung. Für die Richtigkeit der an FinanzOnline übermittelten Daten ist der Betreiber der Kasse verantwortlich.*
