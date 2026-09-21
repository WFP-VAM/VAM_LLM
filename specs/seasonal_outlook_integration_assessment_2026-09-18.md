# Verifica preliminare: Seasonal Outlook Drafter nella UNIFIED APP

Data: 18 settembre 2026. Stato: **audit storico superato dal piano approvato**. Le scelte Kimi/GLM riportate sotto descrivono la verifica precedente. L'implementazione corrente usa Gemini 3.1 Pro per tutte le chiamate: vedere `seasonal_outlook_implementation.md` per architettura, configurazione, verifiche e rilascio. Il deployment aziendale resta da collaudare.

**Esito aggiornato:** le funzioni del prototipo sono compatibili con lo stack dell’app, ma il trasferimento richiede adattamenti a provider, persistenza ed esecuzione. Per l’integrazione, su decisione dell’utente, **GLM 5.2 sostituisce GLM-5.3** nella stesura e revisione del report; l’endpoint selezionato è Vertex MaaS **glm-5.2-maas**, globale e serverless. **Kimi K3** resta il modello per estrazione e revisione delle evidenze: Google ne documenta un percorso di deployment su Vertex da qualificare. La presenza in Model Garden e la disponibilità come API serverless sono verifiche distinte.

Su indicazione dell’utente, la UNIFIED APP locale è il riferimento della versione distribuita su GCP. L’account GCP personale è escluso dall’analisi dell’infrastruttura aziendale. Nessuna inferenza, attivazione API, accettazione di condizioni commerciali, modifica IAM, creazione di risorse o download di pesi è stato eseguito.

## 1. Versione del prototipo da integrare

La directory corretta è **MVP_ver2**, nella root Seasonal Outlook VAM LLM. Il riferimento operativo è **ui_streamlit**, insieme ai moduli condivisi in workflow e alle regole in rules. I runner sperimentali storici non rappresentano l’ultima versione della UI.

Sono stati letti AGENTS.md, MEMORIA_PROGETTO.md, il precedente rapporto sui modelli, configurazione, grafi, client, contratti, storage, recovery, UI ed export. Le raccomandazioni di modelli nel rapporto del 16 settembre non sono state scambiate per decisioni di deployment.

Configurazione del prototipo locale rilevata durante l’audit, precedente alla scelta del modello per l’integrazione:

| Ruolo | Modello Together | Reasoning | Budget output |
|---|---|---|---|
| Estrazione, revisione visiva, riscrittura e feedback umano | moonshotai/Kimi-K3 | high | 32.768 token |
| Prima bozza, revisione testuale, bozza finale | zai-org/GLM-5.3 | high | 65.536 token |

**Configurazione destinazione per l’integrazione:** estrazione con Kimi K3; prima bozza, revisione testuale e bozza finale con **GLM 5.2 tramite Vertex MaaS**. Il budget massimo di output GLM deve essere **64.000 token**, entro il limite documentato dell’endpoint. La modalità thinking va mappata e verificata sull’API Vertex; il valore Together `high` non è assunto equivalente senza collaudo. Questa decisione aggiorna la specifica di integrazione; il prototipo locale non è stato modificato.

Timeout di lettura iniziale 600 secondi; la UI permette 600/1.200/1.800 secondi nella ripresa Kimi. Un tentativo tecnico nella configurazione corrente, retry SDK disabilitati. Il timeout di lettura non è un tetto assoluto al tempo totale della fase.

Fonti: [configurazione](<C:/Users/eugen/Documenti AI/Seasonal Outlook VAM LLM/MVP_ver2/config.json>), [Settings](<C:/Users/eugen/Documenti AI/Seasonal Outlook VAM LLM/MVP_ver2/workflow/config.py:11>), [provider UI](<C:/Users/eugen/Documenti AI/Seasonal Outlook VAM LLM/MVP_ver2/ui_streamlit/provider.py>).

Il flusso da conservare è:

1. Upload manuale delle mappe, scelta esplicita di regione e data, classificazione dei prodotti e controlli di disponibilità.
2. Kimi: estrazione V1 → revisione visiva → evidenze V2.
3. Pausa dell’analista: mappe ed evidenze affiancate, confronto fra versioni e commenti liberi.
4. Eventuale revisione Kimi dai commenti → nuova versione → nuova pausa. Il ciclo può ripetersi.
5. Conferma esplicita della versione da utilizzare.
6. GLM: prima bozza → revisione → bozza finale.
7. Esportazione Word, eventuale appendice con mappe, ZIP e storico.

Percorso normale: **sei chiamate**, più una per ogni revisione richiesta dall’analista. Le riprese sono selettive: revisione Kimi + V2, sola V2 oppure sola bozza finale GLM. Nessuna chiamata deve partire dal semplice rendering della pagina.

I contratti più recenti sono **kimi_evidence_v2** e **glm_report_v2**. Python assegna gli identificativi delle evidenze; le revisioni elencano problemi e incertezze. GLM riceve le evidenze confermate, calendari e regole: non riceve immagini o i commenti originali dell’analista. Il target editoriale è circa 350 parole, non un limite rigido già imposto dall’export.

Fonti: [grafi per fase](<C:/Users/eugen/Documenti AI/Seasonal Outlook VAM LLM/MVP_ver2/ui_streamlit/agent/graph.py:24>), [worker](<C:/Users/eugen/Documenti AI/Seasonal Outlook VAM LLM/MVP_ver2/ui_streamlit/worker.py>), [contratto evidenze](<C:/Users/eugen/Documenti AI/Seasonal Outlook VAM LLM/MVP_ver2/ui_streamlit/agent/evidence_contract.py>), [contratto report](<C:/Users/eugen/Documenti AI/Seasonal Outlook VAM LLM/MVP_ver2/ui_streamlit/agent/report_contract.py>).

## 2. Disponibilità dei modelli su Vertex

Verifica aggiornata il 18 settembre 2026 dopo le schermate della console fornite dall’utente. Sono stati ricontrollati documentazione Google, repository ufficiale dei notebook e schede del produttore. Nessun accesso al progetto aziendale o richiesta di inferenza. Le pagine Google consultate reindirizzano alcuni vecchi URL Vertex alla documentazione Gemini Enterprise Agent Platform.

**Correzione della verifica iniziale:** l’assenza di un modello dalla lista delle API MaaS non implica assenza da Model Garden. Quest’ultimo comprende anche modelli da distribuire su endpoint propri. Google documenta separatamente i [modelli serverless MaaS](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/maas/use-open-models) e il [deployment dei modelli da Model Garden](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-garden/use-models).

| Modello selezionato per l’integrazione | Riscontro aggiornato | Punto ancora da qualificare |
|---|---|---|
| Kimi K3 | Scheda nella console con «Open Notebook» e notebook ufficiale Google per il deployment del modello esatto moonshotai/Kimi-K3 su Vertex. | Configurazione multi-host con GPU; input immagini e contratti Seasonal non provati dall’esempio testuale del notebook. |
| GLM 5.2 | API gestita Vertex MaaS, model ID glm-5.2-maas, regione global, Standard PayGo. | Abilitazione nel progetto aziendale, schema del report, thinking, qualità editoriale e budget output massimo 64.000. |

**Kimi K3: percorso di deployment confermato documentalmente.** Il [notebook ufficiale Google](https://github.com/GoogleCloudPlatform/vertex-ai-samples/blob/main/notebooks/community/model_garden/model_garden_pytorch_kimi_k3_deployment.ipynb), letto senza eseguirlo, usa:

- modello base moonshotai/Kimi-K3, identificativo Model Garden publishers/moonshotai/models/kimi-k3@kimi-k3;
- container SGLang e modello ausiliario RadixArk/Kimi-K3-DSpark per speculative decoding;
- due nodi a4-highgpu-8g, ciascuno con otto NVIDIA B200: **16 GPU B200 nella configurazione proposta**;
- una replica multi-host, con minReplicaCount e maxReplicaCount pari a uno; serving multi-host indicato come Preview e supporto B200 preliminare;
- endpoint Vertex AI Prediction, credenziali Google e pesi pre-caricati su GCS oppure percorsi GCS propri.

Le 16 GPU sono la configurazione del notebook, **non un minimo universale dimostrato**. L’esempio di inferenza passa soltanto testo e non dimostra il funzionamento del nostro payload multimodale, dello schema JSON o dei budget output. La disponibilità di quota/capacità nella regione aziendale rimane da verificare. Questo percorso rispetterebbe il requisito di chiamate attraverso Vertex, ma aggiunge risorse di serving separate dal container dell’app. Google addebita le risorse usate per distribuire un modello su endpoint; non va stimato come semplice consumo di token su API condivisa. [Pricing Model Garden](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-garden/explore-models#pricing)

**GLM 5.2: scelta per l’integrazione confermata dall’utente.** La [scheda Google](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/maas/zaiorg/glm-52) documenta input/output testuali, structured output e thinking. Il ruolo GLM nel workflow riceve soltanto evidenze confermate, calendario e regole: il supporto testuale è coerente con questo contratto. La sostituzione richiede comunque una prova comparativa sui report di riferimento. Non richiede un deployment GPU proprio per GLM.

**API gestite/serverless:** il catalogo MaaS pubblico ricontrollato conferma GLM 5.2. Per Kimi K3 resta verificato il percorso di deployment, mentre l’API serverless non è confermata dal catalogo consultato. Kimi K2 Thinking, presente nella lista MaaS, è testuale e non sostituisce l’estrattore visivo K3. [Catalogo MaaS](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/maas/use-open-models)

Ulteriori vincoli verificati:

- **GLM 5.2** supporta testo, thinking e structured output; è Preview e global. La scheda indica massimo **64.000 token output**, inferiore ai 65.536 richiesti dal prototipo. La disponibilità minima dichiarata è fino all’**8 ottobre 2026**, estendibile: non è una data certa di ritiro. [Scheda ufficiale GLM 5.2](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/maas/zaiorg/glm-52)
- **Kimi K2 Thinking, GLM 5 e GLM 4.7 MaaS** risultano deprecati, con ritiro previsto il **21 ottobre 2026**. Non costituiscono una base opportuna per una nuova integrazione. [Deprecazioni ufficiali](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/deprecations/open-models)
- L’app locale usa **gemini-2.5-pro**. La scheda Google ne indica il ritiro il **20 ottobre 2026**. Offre immagini, structured output e 65.536 token output, ma la sua vicinanza al ritiro richiede di scegliere una versione con orizzonte adeguato prima di usarla come base del nuovo workflow. È una dipendenza da considerare anche nella manutenzione degli altri workflow. [Scheda ufficiale Gemini 2.5 Pro](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/2-5-pro)

**Qualificazione del serving.** Per Kimi K3 il notebook Google fornisce una configurazione concreta da valutare: runtime, regione, quote, formato dei pesi, licenze, latenza, immagini, schema e costo. Per GLM 5.2 la verifica riguarda l’accesso all’API gestita e la compatibilità funzionale dei contratti. Nessun deployment o dimensionamento economico è stato eseguito.

Fonti: [model card Kimi K3](https://huggingface.co/moonshotai/Kimi-K3), [GLM 5.2 su Vertex](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/maas/zaiorg/glm-52), [container personalizzati su Vertex](https://docs.cloud.google.com/vertex-ai/docs/predictions/use-custom-container).

La configurazione da portare nel piano è:

| Ruolo | Modello e percorso Vertex | Stato |
|---|---|---|
| Estrazione e revisione delle evidenze | Kimi K3, endpoint da distribuire seguendo il notebook Google | Modello mantenuto; capacità, costo e compatibilità multimodale da qualificare. |
| Stesura e revisione del report | GLM 5.2, API gestita glm-5.2-maas in global | Sostituzione scelta dall’utente; accesso aziendale e contratti da collaudare. |

Il piano deve prevedere due configurazioni provider distinte, entrambe instradate attraverso Vertex.

## 3. Conversione delle chiamate ai modelli

Il client attuale costruisce richieste Together in formato chat: immagini base64, schema JSON strict, schema ripetuto nel system prompt, reasoning_effort, max_tokens e context_length_exceeded_behavior. Il parser si aspetta choices/message/content, finish_reason e usage.

Il client condiviso dell’app usa invece **ChatVertexAI**, un modello globale, temperatura zero, timeout massimo 600 secondi e due retry predefiniti. Non basta inserirvi i due ID Together: cambiano instradamento, capacità e parametri, e si perderebbero le impostazioni distinte per estrazione e drafting.

Fonte: [richieste Together](<C:/Users/eugen/Documenti AI/Seasonal Outlook VAM LLM/MVP_ver2/workflow/together_client.py>), [client Vertex condiviso](<C:/Users/eugen/VAM LLM Materials/vam-llm/VAM LLM - Prototypes/UNIFIED APP/app/shared/llm.py:195>).

Servirà un adattatore del workflow con configurazioni separate per i due ruoli. Deve:

- usare l’identità del servizio GCP con rinnovo delle credenziali;
- convertire immagini e richieste strutturate nel formato del preciso endpoint scelto;
- mappare il reasoning senza presumere equivalenza fra livelli con lo stesso nome;
- applicare i budget per ruolo, riducendo quello GLM da 65.536 a un massimo di 64.000 token; conservare conteggi di tentativi e classificazione distinta di timeout, connessione, quota, rifiuto, troncamento e schema invalido;
- normalizzare risposta e usage per i contratti interni e l’osservabilità;
- conservare i validatori locali anche quando il provider applica uno schema.

Vertex MaaS espone anche API compatibili con il formato OpenAI: questa compatibilità di protocollo può facilitare l’adattamento, ma non rende automaticamente disponibile un modello. [API MaaS ufficiali](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/maas/call-open-model-apis)

Per Gemini, la documentazione degli structured output descrive un sottoinsieme dello schema e possibili errori con schemi complessi. Gli enum dinamici, i riferimenti, le liste vincolate e i campi non vuoti del prototipo vanno provati contro l’endpoint scelto. Le regole di Gemini non vanno attribuite automaticamente agli endpoint GLM. [Structured output](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/multimodal/control-generated-output)

L’abilitazione di un modello aperto può richiedere accessi e condizioni specifiche, oltre ai permessi già presenti per Gemini. La disponibilità pubblica non prova l’abilitazione nel progetto aziendale. [Accesso ai modelli aperti](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/maas/grant-access-open-models)

## 4. Compatibilità con la UNIFIED APP e GCP

Il Dockerfile usa Python 3.11; start.sh avvia **solo Streamlit su Home.py**. Le richieste della UI sono smistate dal dispatcher Python locale. FastAPI è presente come interfaccia parallela, ma nginx e supervisord non vengono avviati dal percorso Docker corrente. Aggiungere soltanto un router FastAPI non integrerebbe la nuova pagina nel runtime attuale.

Cloud Build costruisce/pubblica l’immagine; il suo YAML non definisce memoria, CPU, concorrenza, timeout o service account del servizio. europe-west1 è una sostituzione del build, non prova da sola la regione dell’inferenza Vertex.

Fonti: [Dockerfile](<C:/Users/eugen/VAM LLM Materials/vam-llm/VAM LLM - Prototypes/UNIFIED APP/Dockerfile:1>), [avvio effettivo](<C:/Users/eugen/VAM LLM Materials/vam-llm/VAM LLM - Prototypes/UNIFIED APP/start.sh:6>), [dispatcher](<C:/Users/eugen/VAM LLM Materials/vam-llm/VAM LLM - Prototypes/UNIFIED APP/app/streamlit_backend/dispatcher.py:2814>), [Cloud Build](<C:/Users/eugen/VAM LLM Materials/vam-llm/VAM LLM - Prototypes/UNIFIED APP/cloudbuild.yaml:1>).

| Componente | Compatibilità | Adattamento richiesto |
|---|---|---|
| Python, LangGraph, Pydantic | Compatibili con lo stack | Consolidare dipendenze e verificare nel container Linux/Python 3.11. Il prototipo e l’app hanno ambienti e pin diversi. |
| Regole, calendari e controlli scientifici | Riutilizzabili | Includere rules/v1.json, kimi_v2.json, glm_v2.json e il calendario oggi letto dalla directory superiore al prototipo. |
| Worker | Non portabile così com’è | Il percorso punta a .venv/Scripts/python.exe; separare il comando Linux dal lancio Windows. |
| Stato e storico | Non durevoli sul filesystem standard Cloud Run | Salvare stato/versioni/conferme in database e mappe/output in object storage. |
| Lock e doppio clic | Protezione solo locale | Sostituire FileLock, PID e .active.json con prenotazioni atomiche e scadenza per run/fase. |
| Pausa umana | Compatibile | Persistente e riprendibile, senza un processo che resti in attesa dell’analista. |
| Elaborazioni lunghe | Compatibili con GCP, con scelte operative | Verificare CPU/lifecycle; valutare worker Cloud Run Job separati. |
| Upload, Word, ZIP | Compatibili | Adeguare limiti, memoria e lettura degli artefatti da GCS. |
| Sessioni e accesso allo storico | Da definire per più utenti | Stabilire proprietà/condivisione di run e autorizzazione alla conferma; un run ID non equivale ad autorizzazione. |
| Cloud SQL/prezzi/DataBridges | Nessuna nuova dipendenza funzionale Seasonal rilevata | Il prototipo parte da mappe caricate manualmente; non richiede recupero automatico di mappe né dati prezzi. |

**Persistenza.** L’app contiene già backend Firestore/GCS e un’implementazione MFI con checkpoint, transazioni, lease e protezione dai risultati di worker scaduti. Sono basi riutilizzabili, ma il comportamento predefinito senza configurazione è **memory**. Anche alcune condizioni di configurazione del servizio condiviso possono comportare fallback in memoria. Non è possibile promettere storico e ripresa dopo riavvio soltanto perché le librerie GCP sono installate. Per Seasonal, se la persistenza richiesta non è disponibile, il comportamento deve essere esplicito.

Il tipo di stato condiviso comprende pending/running/completed/failed; le fasi Seasonal aggiungono prepared/awaiting_analyst/interrupted. Occorre una proiezione coerente fra stato dell’intera analisi e stato della singola operazione. Il polling esistente ha un timeout di 1.800 secondi: non è adatto ad attendere la pausa umana.

Fonti: [selezione storage](<C:/Users/eugen/VAM LLM Materials/vam-llm/VAM LLM - Prototypes/UNIFIED APP/app/shared/async_runs.py:118>), [checkpoint MFI](<C:/Users/eugen/VAM LLM Materials/vam-llm/VAM LLM - Prototypes/UNIFIED APP/app/services/mfi_drafter/execution.py:148>), [prenotazioni MFI](<C:/Users/eugen/VAM LLM Materials/vam-llm/VAM LLM - Prototypes/UNIFIED APP/app/services/mfi_drafter/execution.py:194>), [polling UI](<C:/Users/eugen/VAM LLM Materials/vam-llm/VAM LLM - Prototypes/UNIFIED APP/streamlit_shared.py:970>), [worker e lock locali](<C:/Users/eugen/Documenti AI/Seasonal Outlook VAM LLM/MVP_ver2/ui_streamlit/jobs.py:45>).

**Durata delle fasi.** La pausa umana deve terminare una fase; una successiva azione esplicita avvia la seguente. Per un’integrazione affidabile considero appropriati worker Cloud Run Jobs che leggono snapshot persistenti e terminano a fine fase. È un’opzione architetturale per il piano, non una risorsa già verificata nel deployment. La documentazione del repository descrive già questo meccanismo per il refresh prezzi, senza provarne l’effettiva attivazione.

Riutilizzare i thread dell’attuale servizio rimane una possibilità per un pilot controllato, subordinata alla configurazione CPU/lifecycle e a checkpoint durevoli. Non garantisce che il lavoro continui dopo la chiusura della sessione o un riavvio. Google documenta restrizioni al lavoro in background con CPU legata alle richieste; i servizi hanno timeout HTTP massimo 60 minuti, mentre i Job CPU possono avere task fino a sette giorni. Questi limiti sono distinti dal timeout della chiamata al modello. [Background Cloud Run](https://docs.cloud.google.com/run/docs/tips/general), [timeout servizi](https://docs.cloud.google.com/run/docs/configuring/request-timeout), [timeout Job](https://docs.cloud.google.com/run/docs/configuring/task-timeout)

**Mappe e memoria.** Il codice attuale consente 1–12 immagini, 50 MB totali e 45 milioni di pixel per immagine. Alcune note storiche parlano di sette immagini: per l’integrazione fa fede il codice corrente. A 50 MB, il solo base64 occupa circa 66,7 MB, prima di copie JSON, file temporanei, immagini decodificate ed export. Il filesystem Cloud Run consuma memoria ed è effimero. Non è quindi sufficiente dimensionare il servizio sulla sola dimensione dell’upload. [Contratto Cloud Run](https://docs.cloud.google.com/run/docs/container-contract)

Cloud Run applica 32 MiB per richiesta HTTP/1: il limite è per richiesta, non per pacchetto complessivo di file. Serve un limite per file coerente o upload diretto a GCS per i file grandi. Anche i limiti d’ingresso del modello selezionato vanno verificati; ad esempio Gemini 2.5 Pro documenta 7 MB per immagine inline e 30 MB da GCS. [Quote Cloud Run](https://docs.cloud.google.com/run/quotas), [limiti immagini Gemini](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/2-5-pro)

Non emergono esigenze di GPU per l’orchestrazione e la UI se si usano modelli remoti gestiti. GPU e gestione del serving diventano un tema separato solo nell’ipotesi di ospitare i pesi autonomamente.

## 5. UI: elementi da portare nell’app

È stata ispezionata nel browser la UI locale su porta 8502: preparazione input, storico, esempio AFY e visualizzazione delle evidenze accanto alle mappe. Non sono stati premuti pulsanti di inferenza. La UNIFIED APP non rispondeva sulla porta locale 8501; il suo confronto è stato eseguito sul codice.

La nuova pagina può adottare tema, logo, navigazione, onboarding e segnalazione problemi condivisi, mantenendo dal prototipo:

- preparazione con regione non preselezionata, calendario e data limite;
- selettore della mappa, immagine ingrandibile e schede leggibili con legenda, periodo, segnali e limiti;
- versioni e differenze, commenti liberi e conferma esplicita della versione;
- tre schede: evidenze/revisione, report/download, pacchetto input;
- progressi, diagnosi e riprese selettive;
- storico delle analisi ed esempi chiaramente etichettati come storici.

L’integrazione deve passare per il dispatcher usato dalla UI e, se si mantiene la parità API, dal relativo router. Servono chiavi sessione/query specifiche Seasonal e accesso agli artefatti tramite storage, al posto dei percorsi locali. Evitare di duplicare la sidebar autonoma del prototipo o copiare dipendenze da cartelle esterne al container.

Fonti: [UI prototipo](<C:/Users/eugen/Documenti AI/Seasonal Outlook VAM LLM/MVP_ver2/ui_streamlit/app.py>), [viste evidenze/report](<C:/Users/eugen/Documenti AI/Seasonal Outlook VAM LLM/MVP_ver2/ui_streamlit/views.py>), [pagina MFI](<C:/Users/eugen/VAM LLM Materials/vam-llm/VAM LLM - Prototypes/UNIFIED APP/pages/4_MFI_Drafter.py:22>), [home](<C:/Users/eugen/VAM LLM Materials/vam-llm/VAM LLM - Prototypes/UNIFIED APP/streamlit_app.py>).

Nel repository risultano **due validator e due drafter**. Seasonal sarebbe il **terzo drafter e quinto servizio complessivo**; il riferimento dell’utente al “quarto” resta una questione di denominazione/ordinamento da chiarire nel piano.

## 6. Verifiche e limiti osservati

I quattro XML di collaudo Kimi v2 esistenti sono stati letti: **58 backend/GLM + 19 regressioni Kimi + 13 UI + 24 compatibilità storica = 114 test**, zero errori/fallimenti registrati. Sono risultati salvati del collaudo precedente, **non test rieseguiti durante questo audit** e non prove Vertex.

Il nuovo dato reale del 18 settembre integra le note del 17: run d94c91e1e69246ffbc84aa823d05f5ec, contratto kimi_evidence_v2. Estrazione riuscita in circa **337,5 secondi**, sei mappe e 35 segnali, validazione strutturale positiva. Revisione interrotta dopo circa **19,1 secondi** con APIConnectionError/RemoteProtocolError, senza una risposta completa. Questo attesta un primo passaggio reale del nuovo contratto; non certifica l’intero ciclo, non prova accuratezza scientifica e non consente di attribuire con certezza la causa alla complessità del prompt o al provider.

Gli otto record UI presenti mostravano stato finale failed, con versioni diverse del codice e cause diverse. Non sono un campione controllato per stimare un tasso di errore. Gli esempi storici completati dichiarano recuperi tecnici. Il passaggio a Vertex non risolve automaticamente errori semantici, risposte incomplete o vincoli scientifici.

Fonti: [verifica salvata](<C:/Users/eugen/Documenti AI/Seasonal Outlook VAM LLM/MVP_ver2/ui_streamlit/verification.json>), [ultima estrazione](<C:/Users/eugen/Documenti AI/Seasonal Outlook VAM LLM/MVP_ver2/ui_streamlit/runs/d94c91e1e69246ffbc84aa823d05f5ec/attempts/712f387a158643ab977c5da9a01c2edb/transport_call_01.json>), [ultima revisione](<C:/Users/eugen/Documenti AI/Seasonal Outlook VAM LLM/MVP_ver2/ui_streamlit/runs/d94c91e1e69246ffbc84aa823d05f5ec/attempts/712f387a158643ab977c5da9a01c2edb/transport_call_02.json>).

Non sono stati eseguiti test di inferenza, nuovi test automatici o build Linux. Il precedente collaudo documenta export Word verificati strutturalmente, con rendering visivo non completato. Questo limite rimane da chiudere nel collaudo dell’integrazione.

## 7. Decisioni necessarie per il piano successivo

**La scelta del modello di drafting è definita: GLM 5.2 su Vertex MaaS sostituisce GLM-5.3.** Per Kimi K3 resta da qualificare il notebook ufficiale, soprattutto immagini/schema e sostenibilità delle risorse. Per GLM 5.2 vanno verificati accesso aziendale, schema, thinking e qualità dei report, con massimo 64.000 token output. L’identità della precedente scheda GLM 5.3 non è più un prerequisito del piano. La scelta dei modelli è registrata nella specifica; l’implementazione non è iniziata.

Poi occorre definire:

1. Persistenza richiesta per storico, pausa umana e ripresa dopo riavvio; Firestore/GCS sono già supportati nel codice, ma la loro configurazione aziendale non è verificata.
2. Esecuzione: pilot nel servizio corrente o worker separato per fase, con idempotenza e retry coordinati per evitare chiamate duplicate.
3. Proprietà e visibilità delle analisi fra utenti; nessuna migrazione automatica dei run locali è stata assunta.
4. Qualificazione del preciso endpoint: immagini, schema, reasoning, output, latenza, errori e fedeltà delle evidenze sui casi di riferimento.
5. Collaudo Linux/GCP e prova di ripresa su una diversa istanza, seguiti da revisione scientifica dei report.

Memoria/CPU, concorrenza, timeout del servizio, policy IAM, regione effettiva dei modelli e quote restano parametri del deployment aziendale non presenti nel repository. Non sono stati inferiti dall’account personale.

Questo documento costituisce la base per discutere l’implementazione; non è un piano approvato e non introduce modifiche al comportamento dell’app.
