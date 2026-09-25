# Attivare Seasonal Outlook dalla console GCP

Procedura manuale per il progetto aziendale, aggiornata il 25 settembre 2026. Non richiede Terraform né Cloud Shell. Il codice abilita Seasonal per impostazione predefinita, ma il solo aggiornamento dell'immagine dell'app non crea le risorse necessarie.

Dal refactoring di settembre 2026 le fasi di Seasonal (estrazione, feedback, report) vengono eseguite **dal servizio web stesso**, in un thread in background, come per Market Monitor e MFI. Non esiste più un Cloud Run Job. Chi ha già configurato Seasonal con il Job segua anche la sezione 8.

## 1. Annotare i valori del deployment

Aprire il progetto aziendale nella console. In **Cloud Run → Services**, aprire l'app VAM LLM e annotare la regione e il **service account della revisione attuale**. Conservare questa identità per il servizio web.

| Valore | Cosa usare |
|---|---|
| `PROJECT_ID` | ID del progetto aziendale, non il nome visualizzato o il numero |
| `REGION` | Regione del bucket; preferibilmente quella dell'app, nel rispetto delle policy aziendali |
| `APP_SERVICE_ACCOUNT` | Email del service account attuale dell'app |
| `DATABASE_ID` | ID esatto del database Firestore Native esistente; anche `(default)` è valido |
| `BUCKET_NAME` | Nome globalmente univoco, ad esempio `PROJECT_ID-seasonal-outlook` |
| `SIGNER_EMAIL` | `seasonal-outlook-worker@PROJECT_ID.iam.gserviceaccount.com` (identità che firma i link di download; il nome storico resta `worker`) |
| `IMAGE_DIGEST` | Immagine della nuova build, identificata con `@sha256:...` |

I valori in maiuscolo sono segnaposto da sostituire. L'app non ricava questi dati dall'account personale e non occorrono chiavi JSON.

In **APIs & Services → Library**, verificare/abilitare Cloud Firestore API, Cloud Storage API, Vertex AI API, Identity and Access Management (IAM) API e IAM Service Account Credentials API. La build continua a usare la pipeline e Artifact Registry già presenti.

## 2. Creare identità, bucket e permessi di base

In **IAM**, assegnare a `APP_SERVICE_ACCOUNT` sul progetto `Vertex AI User` (`roles/aiplatform.user`) e `Cloud Datastore User` (`roles/datastore.user`), preservando i permessi già presenti. Il ruolo Vertex serve perché il servizio web esegue le chiamate a Gemini; se `SEASONAL_PROJECT` coincide con il progetto usato da Market Monitor e MFI (`VERTEX_PROJECT_ID`), l'app potrebbe averlo già.

In **IAM & Admin → Service Accounts → Create service account**, creare `seasonal-outlook-worker` se non esiste. Non esegue più codice: serve solo come identità di firma per i link di download. [Guida Google ai service account](https://docs.cloud.google.com/iam/docs/service-accounts-create).

In **Cloud Storage → Buckets → Create**, creare `BUCKET_NAME`, classe Standard, località coerente con `REGION`, **Uniform bucket-level access** e **Public access prevention**. Nel pannello **Permissions** del bucket aggiungere `APP_SERVICE_ACCOUNT` con `Storage Object User` (`roles/storage.objectUser`) e `SIGNER_EMAIL` con almeno `Storage Object Viewer` (`roles/storage.objectViewer`): un link firmato dà accesso con i permessi dell'identità che lo firma. Conservare mappe e storico senza regole automatiche di cancellazione. [Creazione di bucket](https://docs.cloud.google.com/storage/docs/creating-buckets).

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

## 4. Creare il ruolo di firma

In **IAM & Admin → Roles → Create role**, nel progetto aziendale creare il ruolo seguente, con esattamente il permesso indicato. [Ruoli personalizzati](https://docs.cloud.google.com/iam/docs/creating-custom-roles).

| ID | Titolo | Permessi |
|---|---|---|
| `seasonalArtifactSigner` | Seasonal Artifact Signer | `iam.serviceAccounts.signBlob` |

Aprire il service account `seasonal-outlook-worker` → **Permissions / Manage access** → **Grant access**. Aggiungere come principal `APP_SERVICE_ACCOUNT` e assegnare `Seasonal Artifact Signer`. Questo consente all'app di firmare i download usando l'identità di firma, che ha accesso in lettura al bucket.

## 5. Generare l'immagine

Dopo il push, nella console **Cloud Build → Triggers** eseguire il trigger già usato per l'app, se non parte automaticamente. Attendere una build riuscita e copiare da Artifact Registry il digest dell'immagine prodotta. Il `cloudbuild.yaml` nel repository costruisce e pubblica l'immagine; non contiene il deployment del servizio. Eventuali automatismi aziendali esterni non sono stati verificati.

## 6. Configurare il servizio web

Aprire **Cloud Run → Services → app VAM LLM → Edit & deploy new revision**. Selezionare `IMAGE_DIGEST` e, nella scheda **Variables & Secrets**, aggiungere queste voci conservando le altre variabili, i secret, il service account e le impostazioni esistenti:

| Variabile | Valore |
|---|---|
| `SEASONAL_DRAFTER_ENABLED` | `true` |
| `SEASONAL_PROJECT` | `PROJECT_ID` |
| `SEASONAL_BUCKET` | `BUCKET_NAME`, senza `gs://` |
| `SEASONAL_DATABASE` | `DATABASE_ID` |
| `SEASONAL_COLLECTION` | `seasonal_outlook_runs` |
| `SEASONAL_PREFIX` | `seasonal-outlook` |
| `SEASONAL_SIGNER` | `SIGNER_EMAIL` |
| `SEASONAL_MODEL` | `gemini-3.1-pro-preview` |
| `SEASONAL_LOCATION` | `global` |

`SEASONAL_LOCATION` è la località di Gemini (`global`), diversa dalla regione del servizio. L'autenticazione avviene con le identità di servizio. Sostituire un eventuale `SEASONAL_DRAFTER_ENABLED=false` esplicito: il default nel codice non lo sovrascrive.

Nella stessa revisione verificare le impostazioni di esecuzione, necessarie perché le fasi girano in background dopo la risposta alla pagina:

- **CPU always allocated** (CPU sempre allocata), già richiesta da Market Monitor e MFI;
- **memoria** sufficiente per gli export Word e ZIP con fino a 12 mappe: il Job precedente usava 4 GiB, quindi aumentare la memoria del servizio se è inferiore;
- **minimum instances** ≥ 1 consigliato: se un'istanza viene arrestata durante una fase, quella fase risulta interrotta dopo la sua scadenza e va rilanciata con **Retry failed operation**.

Dopo che risorse, permessi e indici sono pronti, distribuire la revisione con la normale procedura aziendale. [Variabili del servizio](https://docs.cloud.google.com/run/docs/configuring/services/environment-variables).

## 7. Verificare l'attivazione

Aprire Seasonal Outlook Drafter: la preparazione degli input deve essere disponibile e non devono comparire errori di configurazione. Questo conferma le impostazioni locali del servizio, non verifica ancora tutti i permessi o la disponibilità del modello.

Avviare una prima analisi e seguirne l'avanzamento nella pagina e nei **Logs** del servizio web, fino alla revisione umana. Provare feedback, conferma e download. Poi completare i casi AFY, AMX e ASE e provare un nuovo tentativo dopo un errore (**Operations and review decisions → Retry failed operation**), come descritto nella [specifica di implementazione](../../specs/seasonal_outlook_implementation.md). Gli eventuali errori Vertex relativi a accesso, quota o disponibilità del modello vanno risolti nel progetto aziendale.

Per fermare nuovi avvii, impostare `SEASONAL_DRAFTER_ENABLED=false` sul servizio web e distribuire la revisione: storico e download rimangono disponibili. Non cancellare bucket o database per disabilitare il workflow.

## 8. Aggiornare una configurazione con il Job

Se Seasonal era già attivo con il Cloud Run Job:

1. Assegnare `Vertex AI User` a `APP_SERVICE_ACCOUNT` (sezione 2), **prima** di distribuire la nuova revisione.
2. Distribuire la nuova revisione (sezione 6) e rimuovere dal servizio le variabili `SEASONAL_JOB` e `SEASONAL_JOB_REGION`, non più lette.
3. Solo dopo il collaudo della nuova revisione, eliminare il Job `seasonal-outlook-worker` in **Cloud Run → Jobs**, l'assegnazione di `Seasonal Job Dispatcher` all'app e poi il ruolo stesso. Chi usa Terraform applica `main.tf` in questo momento: rimuove Job e ruolo e sposta il ruolo Vertex dal worker all'app.
4. Facoltativo: rimuovere da `seasonal-outlook-worker` i ruoli `Vertex AI User` e `Cloud Datastore User`, non più necessari. Mantenere l'accesso in lettura al bucket e il ruolo `Seasonal Artifact Signer` assegnato all'app.

Le analisi create prima dell'aggiornamento restano nello storico ma non possono più essere aperte. Per tornare indietro basta reindirizzare il traffico alla revisione precedente, finché il Job non è stato eliminato; le analisi create con la nuova versione non si aprono nella precedente.

Verifica locale del 25 settembre 2026: **38 test Seasonal superati**. Build Linux e collaudo GCP rimangono da eseguire nell'ambiente aziendale (fase 7 del [piano di refactoring](../../specs/coherence_refactor_plan.md)). Nessuna risorsa GCP è stata creata o modificata durante il refactoring.
