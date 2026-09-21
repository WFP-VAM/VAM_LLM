# Attivare Seasonal Outlook dalla console GCP

Procedura manuale per il progetto aziendale, aggiornata il 21 settembre 2026. Non richiede Terraform, Cloud Shell o accesso al progetto da parte di Codex. Il codice abilita Seasonal per impostazione predefinita, ma il solo aggiornamento dell'immagine dell'app non crea le risorse necessarie.

## 1. Annotare i valori del deployment

Aprire il progetto aziendale nella console. In **Cloud Run → Services**, aprire l'app VAM LLM e annotare la regione e il **service account della revisione attuale**. Conservare questa identità per il servizio web.

| Valore | Cosa usare |
|---|---|
| `PROJECT_ID` | ID del progetto aziendale, non il nome visualizzato o il numero |
| `JOB_REGION` | Regione scelta per il Job; preferibilmente quella dell'app, nel rispetto delle policy aziendali |
| `APP_SERVICE_ACCOUNT` | Email del service account attuale dell'app |
| `DATABASE_ID` | ID esatto del database Firestore Native esistente; anche `(default)` è valido |
| `BUCKET_NAME` | Nome globalmente univoco, ad esempio `PROJECT_ID-seasonal-outlook` |
| `WORKER_EMAIL` | `seasonal-outlook-worker@PROJECT_ID.iam.gserviceaccount.com` |
| `IMAGE_DIGEST` | Immagine della nuova build, identificata con `@sha256:...` |

I valori in maiuscolo sono segnaposto da sostituire. L'app non ricava questi dati dall'account personale e non occorrono chiavi JSON.

In **APIs & Services → Library**, verificare/abilitare Cloud Run Admin API, Cloud Firestore API, Cloud Storage API, Vertex AI API, Identity and Access Management (IAM) API e IAM Service Account Credentials API. La build continua a usare la pipeline e Artifact Registry già presenti.

## 2. Creare identità, bucket e permessi di base

In **IAM & Admin → Service Accounts → Create service account**, creare `seasonal-outlook-worker`. In **IAM**, assegnare a questa identità sul progetto `Vertex AI User` (`roles/aiplatform.user`) e `Cloud Datastore User` (`roles/datastore.user`). Verificare che anche `APP_SERVICE_ACCOUNT` abbia `Cloud Datastore User`, preservando i permessi già presenti. [Guida Google ai service account](https://docs.cloud.google.com/iam/docs/service-accounts-create).

In **Cloud Storage → Buckets → Create**, creare `BUCKET_NAME`, classe Standard, località coerente con `JOB_REGION`, **Uniform bucket-level access** e **Public access prevention**. Nel pannello **Permissions** del bucket aggiungere sia `APP_SERVICE_ACCOUNT` sia `WORKER_EMAIL` con `Storage Object User` (`roles/storage.objectUser`). Conservare mappe e storico senza regole automatiche di cancellazione. [Creazione di bucket](https://docs.cloud.google.com/storage/docs/creating-buckets).

## 3. Preparare Firestore

Aprire **Firestore** e selezionare `DATABASE_ID`. Riutilizzare il database Native esistente. Se l'app non dispone di un database Firestore Native, crearne uno **Standard / Native mode**, ad esempio `vam-llm-async`, nella località aziendale scelta e usare il suo ID nelle variabili. Non modificare la modalità di un database esistente. La collection `seasonal_outlook_runs` verrà creata alla prima analisi; non occorre inserire documenti fittizi.

In **Indexes → Composite → Create index**, impostare collection ID `seasonal_outlook_runs`, query scope **Collection** e creare i sette indici seguenti. I campi sono elencati nell'ordine da inserire. Attendere che siano pronti. [Gestione degli indici](https://docs.cloud.google.com/firestore/native/docs/standard-indexing).

| Indice | Campi e direzione |
|---|---|
| Regione | `region_id` Ascending, `created_at` Descending |
| Data | `report_date` Ascending, `created_at` Descending |
| Stato | `status` Ascending, `created_at` Descending |
| Regione e data | `region_id` Ascending, `report_date` Ascending, `created_at` Descending |
| Regione e stato | `region_id` Ascending, `status` Ascending, `created_at` Descending |
| Data e stato | `report_date` Ascending, `status` Ascending, `created_at` Descending |
| Tutti i filtri | `region_id` Ascending, `report_date` Ascending, `status` Ascending, `created_at` Descending |

In **Indexes → Single field → Add exemption**, per la stessa collection disabilitare l'indicizzazione dei campi `operations`, `requests`, `versions`, `maps`, `artifacts`, `input_artifact`, `confirmation`, `notes`, compresi gli indici array quando applicabili. Queste otto esenzioni evitano di indicizzare l'intero contenuto degli audit; lasciare attivi gli indici automatici di `created_at` e dei campi usati nei filtri.

## 4. Creare i due ruoli specifici

In **IAM & Admin → Roles → Create role**, nel progetto aziendale creare i ruoli seguenti, aggiungendo esattamente i permessi indicati. Il ruolo viene definito qui; le assegnazioni si fanno sulle risorse indicate nei passaggi successivi. [Ruoli personalizzati](https://docs.cloud.google.com/iam/docs/creating-custom-roles).

| ID | Titolo | Permessi |
|---|---|---|
| `seasonalJobDispatcher` | Seasonal Job Dispatcher | `run.jobs.run`, `run.jobs.runWithOverrides` |
| `seasonalArtifactSigner` | Seasonal Artifact Signer | `iam.serviceAccounts.signBlob` |

Aprire il service account **del worker** → **Permissions / Manage access** → **Grant access**. Aggiungere come principal `APP_SERVICE_ACCOUNT` e assegnare `Seasonal Artifact Signer`. Questo consente all'app di firmare i download usando l'identità del worker, che ha accesso al bucket.

## 5. Generare l'immagine e creare il Job

Dopo il push, nella console **Cloud Build → Triggers** eseguire il trigger già usato per `phase_3_faster_MFI`, se non parte automaticamente. Attendere una build riuscita e copiare da Artifact Registry il digest dell'immagine prodotta. Il `cloudbuild.yaml` nel repository costruisce e pubblica l'immagine; non contiene la creazione del Job o il deployment del servizio. Eventuali automatismi aziendali esterni non sono stati verificati.

In **Cloud Run → Jobs → Deploy container / Create job**, usare questi valori. Le opzioni si trovano in **Containers, Networking, Security**, **Task capacity** e **Parallelism**. [Creazione di Job dalla console](https://docs.cloud.google.com/run/docs/create-jobs).

| Impostazione | Valore |
|---|---|
| Nome | `seasonal-outlook-worker` |
| Regione | `JOB_REGION` |
| Immagine | `IMAGE_DIGEST`, uguale a quella da distribuire nell'app |
| Task | `1` |
| Parallelismo | `1` |
| CPU / memoria | `2` / `4 GiB` |
| Task timeout | `7200` secondi |
| Retry per task | `0` |
| Service account | `WORKER_EMAIL` |
| Container command | `python` |
| Container arguments | Tre argomenti distinti: `-m`, `app.services.seasonal_outlook.worker`, `--help` |

Inserire sul Job le variabili del passaggio 6. Creare il Job senza scheduler. L'app fornisce gli identificativi di analisi e operazione a ogni avvio; l'esecuzione manuale con gli argomenti iniziali mostra soltanto l'help.

Nel pannello **Permissions** del Job, aggiungere `APP_SERVICE_ACCOUNT` con il ruolo `Seasonal Job Dispatcher`. Il permesso di esecuzione con override è necessario per trasmettere gli identificativi. [Esecuzioni e override](https://docs.cloud.google.com/run/docs/execute/jobs).

## 6. Inserire le variabili su Job e app

Usare gli stessi valori **sia nel Job sia nel servizio web**:

| Variabile | Valore |
|---|---|
| `SEASONAL_DRAFTER_ENABLED` | `true` |
| `SEASONAL_PROJECT` | `PROJECT_ID` |
| `SEASONAL_BUCKET` | `BUCKET_NAME`, senza `gs://` |
| `SEASONAL_DATABASE` | `DATABASE_ID` |
| `SEASONAL_COLLECTION` | `seasonal_outlook_runs` |
| `SEASONAL_PREFIX` | `seasonal-outlook` |
| `SEASONAL_JOB` | `seasonal-outlook-worker` |
| `SEASONAL_JOB_REGION` | `JOB_REGION` |
| `SEASONAL_SIGNER` | `WORKER_EMAIL` |
| `SEASONAL_MODEL` | `gemini-3.1-pro-preview` |
| `SEASONAL_LOCATION` | `global` |

La regione del Job e `SEASONAL_LOCATION` sono impostazioni diverse. Gemini usa `global`. L'autenticazione avviene con le identità di servizio.

Per il servizio web, aprire **Cloud Run → Services → app VAM LLM → Edit & deploy new revision**. Selezionare `IMAGE_DIGEST`, aggiungere queste voci nella scheda **Variables & Secrets** e conservare le altre variabili, i secret, il service account e le impostazioni esistenti. Sostituire un eventuale `SEASONAL_DRAFTER_ENABLED=false` esplicito: il default nel codice non lo sovrascrive. Dopo che risorse, permessi e indici sono pronti, distribuire la revisione con la normale procedura aziendale. [Variabili del servizio](https://docs.cloud.google.com/run/docs/configuring/services/environment-variables).

## 7. Verificare l'attivazione

Aprire Seasonal Outlook Drafter: la preparazione degli input deve essere disponibile e non devono comparire errori di configurazione. Questo conferma le impostazioni locali del servizio, non verifica ancora tutti i permessi o la disponibilità del modello.

Avviare una prima analisi: in Cloud Run deve comparire un'esecuzione del Job; controllarne **Logs** e verificare che il workflow raggiunga la revisione umana. Provare feedback, conferma e download. Poi completare i casi AFY, AMX e ASE e le prove di ripresa descritte nella [specifica di implementazione](../../specs/seasonal_outlook_implementation.md). Gli eventuali errori Vertex relativi a accesso, quota o disponibilità del modello vanno risolti nel progetto aziendale.

Per fermare nuovi avvii, impostare `SEASONAL_DRAFTER_ENABLED=false` sul servizio web e distribuire la revisione: storico e download rimangono disponibili; i Job già avviati possono terminare. Non cancellare bucket o database per disabilitare il workflow.

Verifica locale del 21 settembre: **23 test Seasonal superati**. Build Linux e collaudo GCP rimangono da eseguire nell'ambiente aziendale. Nessuna risorsa GCP è stata creata da Codex.
