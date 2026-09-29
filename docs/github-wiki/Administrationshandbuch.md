# 🛠️ Administrationshandbuch

[🇬🇧 English](Administrator-Guide) · [🇩🇪 Deutsch](Administrationshandbuch) · [Startseite](Startseite-Deutsch)

Folgen Sie diesem Ablauf für jedes Unternehmen und jede Umgebung. Schließen Sie TEST ab und dokumentieren Sie die Abnahme, bevor Sie LIVE vorbereiten.

## 1 · ERPNext-Stammdaten vorbereiten

Prüfen Sie vor dem Anlegen der Fiskaly-Ressourcen:

| Prüfung | Was zu kontrollieren ist |
| --- | --- |
| Unternehmen | Währung **EUR**, Zeitzone **Europe/Vienna**, österreichische Steuerdaten und vollständige Betriebsanschrift. |
| POS-Profil | Zugeordnete Benutzer, Lager, Preisliste und Zahlungsarten stimmen. |
| Zahlungsarten | Jede Zahlungsart ist richtig als RKSV-Bareinnahme oder Nicht-Barzahlung klassifiziert. Prüfen Sie auch Mischzahlungen. |
| Belegdruck | Im POS-Profil ist das geschützte Druckformat **POS Invoice RKSV** hinterlegt. |
| USt-Mappings | Jeder im POS verwendete Steuersatz ist zugeordnet. Lassen Sie die Zuordnung von der Buchhaltung prüfen; setzen Sie keinen steuerpflichtigen Satz auf null, um eine Prüfung zu umgehen. |

Das Kassenformular beschreibt die Zuordnung als **eine Registrierkasse je POS-Profil und Umgebung**. Planen Sie für jedes relevante Profil eine eigene Kasse in TEST und LIVE.

## 2 · TEST-API-Verbindung anlegen

Öffnen Sie **Fiskaly RKSV → API Connections** oder suchen Sie in ERPNext nach **Fiskaly API Connection**.

1. Legen Sie eine Verbindung für das richtige Unternehmen an und wählen Sie **TEST**.
2. Wählen Sie den Provider:
   - **SIGN_AT_V1** für den derzeit unterstützten Produktivpfad.
   - **SIGN_AT_UNIFIED** nur für TEST-Integration; LIVE ist in der App gesperrt.
3. Tragen Sie die TEST-Zugangsdaten in ERPNext ein. Zugangsdaten gehören nicht in Quelldateien, Git, Wiki, Tickets oder Screenshots.
4. Verwenden Sie in TEST nur syntaktisch gültige FinanzOnline-Dummywerte. Keine echten LIVE-Zugangsdaten in TEST eintragen.
5. Speichern Sie die Verbindung und klicken Sie **Verbindung testen**. Beheben Sie Fehler, bevor Sie fortfahren.
6. Bei SIGN_AT_V1 klicken Sie nach dem Speichern auf **FinanzOnline authentifizieren**. Diese Aktion gibt es für Unified nicht.
7. Bei Unified laden Sie Organisation/Scope erst nach dem Speichern der Zugangsdaten und prüfen, ob die Organisation zum API-Key gehört.

Führen Sie Verbindungen getrennt nach Unternehmen, Provider und Umgebung. Verwenden Sie eine provisionierte Verbindung nicht weiter, um eine Kasse zwischen Umgebungen zu verschieben. Kritische Änderungen müssen bestätigt werden; Kassen-Zuordnungen sind nach der Initialisierung gesperrt.

## 3 · TEST-Kasse anlegen und provisionieren

Öffnen Sie **Fiskaly RKSV → Cash Registers** und legen Sie eine Kasse an.

1. Tragen Sie Kassenname, Unternehmen, POS-Profil, API-Verbindung und Betriebsanschrift ein.
2. Prüfen Sie, ob der angezeigte Provider und die Umgebung zur Verbindung passen.
3. Wählen Sie bei SIGN_AT_V1 eine passende bestehende SCU oder die Option, beim Provisionieren eine neue anzulegen. Eine bestehende SCU muss zur gleichen Umgebung und Unternehmensidentität gehören.
4. Ergänzen Sie alle USt-Mappings des POS-Profils.
5. Prüfen Sie die Zuordnungen und klicken Sie einmal auf **Provisionieren und initialisieren**.
6. Prüfen Sie Provider-Kassen-ID, Status, Seriennummer, verknüpften Startbeleg und bei SIGN_AT_V1 dessen FinanzOnline-Prüfstatus.

> 🔒 **Nach der Initialisierung:** Wichtige Zuordnungen werden nach der Initialisierung oder Erstellung des Startbelegs gesperrt. Bei einer falschen Zuordnung keine Verkäufe verarbeiten; zuerst den Fiskaly-Administrator kontaktieren.

## 4 · TEST aktivieren und abnehmen

Öffnen Sie **Fiskaly RKSV → RKSV Settings**.

1. Setzen Sie die aktive Umgebung auf **TEST**.
2. Aktivieren Sie RKSV erst, wenn TEST-Verbindung und Kasse bereit sind.
3. Bestätigen Sie eine kritische Änderung, falls Sie dazu aufgefordert werden.
4. Arbeiten Sie eine kontrollierte Abnahmeliste durch:
   - Barzahlung;
   - Nicht-Barzahlung;
   - Mischzahlung;
   - verwendete Steuersätze und Rundung;
   - Belegdruck und QR-Code;
   - Retoure mit Bezug zur Originalrechnung;
   - Statusüberwachung der Fiskalbelege;
   - Wiederherstellung nach einer simulierten TEST-Unterbrechung, falls vorgesehen.
5. Prüfen Sie, dass Rechnungsbeträge und Fiskalbelegdaten übereinstimmen. Ungeklärte wartende, fehlgeschlagene oder maßnahmenpflichtige Belege müssen vor der Abnahme bearbeitet werden.

Simulieren Sie niemals einen Ausfall an einer LIVE-Kasse.

## 5 · LIVE vorbereiten

Nach erfolgreicher TEST-Abnahme legen Sie eine **eigene LIVE-API-Verbindung und eine eigene LIVE-Kasse** an. Wandeln Sie die TEST-Kasse nicht um.

1. Legen Sie eine SIGN_AT_V1-Verbindung für das richtige Unternehmen an und wählen Sie **LIVE**.
2. Tragen Sie produktive Fiskaly-Zugangsdaten und die berechtigten echten FinanzOnline-Registrierkassen-Webservice-Benutzerdaten ein.
3. Testen Sie die Verbindung und schließen Sie die erforderliche FinanzOnline-Authentifizierung ab.
4. Erstellen Sie eine separate LIVE-Kasse mit richtigem LIVE-POS-Profil, Verbindung, Anschrift, SCU und USt-Mappings.
5. Provisionieren Sie einmal und prüfen Sie LIVE-Startbeleg und FinanzOnline-Status.
6. Stellen Sie erst nach erfolgreicher Prüfung die globale Umgebung unter **RKSV Settings** auf **LIVE**, bestätigen Sie die kritische Änderung und prüfen Sie, dass Verbindung und Kasse ebenfalls LIVE sind.
7. Überwachen Sie den ersten Verkauf und prüfen Sie Beleg, QR-Code und Status.

> ⛔ **Unified LIVE ist nicht verfügbar.** Verwenden Sie die Einstellung `Allow Unified LIVE` nicht als Umgehung; der Provider-Adapter blockiert LIVE technisch.

## Sicherheit und Berechtigungen

- Beschränken Sie API-Verbindungen, Einstellungen, Kassen-Provisionierung, Ausfallbearbeitung und Exporte auf die benötigten Rollen.
- Zugangsdaten niemals im öffentlichen Wiki teilen. Verwenden Sie den freigegebenen privaten Weg zur Geheimnisübermittlung.
- Signaturen, QR-Daten, Belegkennungen, Provider-Snapshots und Lebenszyklusnachweise nicht in der Datenbank ändern.
- Behandeln Sie Fiskalbelege, DEP7-Dateien, API-Protokolle, FinanzOnline-Referenzen und Kundendaten als vertrauliche Betriebsdaten.
- Prüfen Sie vor Änderungen von Umgebung oder Zugangsdaten Unternehmen, Provider und Kassenzuordnung und bestätigen Sie anschließend die kritische Änderung in der App.

