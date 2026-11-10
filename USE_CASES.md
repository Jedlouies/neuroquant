# NeuroQuant System Use Cases

## 1. Purpose and scope

This document describes the use cases supported by the NeuroQuant project as implemented in the desktop workstation and companion mobile app. NeuroQuant supports brain MRI review, automated tumor segmentation and volumetric measurement, patient and analysis record keeping, report export, radiologist scan upload, and password-protected viewing of prepared image previews on mobile.

Automated measurements and AI responses are decision-support outputs. A qualified clinician reviews source images and findings before using them in clinical care.

## 2. Actors

| Actor | Role |
| --- | --- |
| Neurologist / clinician | Signs into the desktop workstation, imports or retrieves a case, reviews images, runs segmentation, reviews findings, and exports reports. |
| Radiologist | Signs into the radiologist workspace and uploads, refreshes, or deletes MRI/CT scan files in cloud storage. |
| Mobile reviewer | Opens a synced segmentation folder with its folder password and views prepared MRI/CT GIF previews. |
| NeuroQuant desktop application | Loads imaging data, runs the segmentation model, calculates measurements, and stores case and report history. |
| Cloud services | Store radiologist scans and synced previews, and provide folder listings and short-lived image links. |
| AI assistant service | Answers optional brain tumor questions in the desktop AI panel when configured and online. |

## 3. System boundary and assumptions

- The desktop application is the primary environment for importing source MRI/CT data and running segmentation.
- MRI segmentation uses the four MRI sequences FLAIR, T1, T1ce, and T2. CT may be loaded as an optional companion scan.
- The model produces a segmentation mask and estimates active tumor core, peritumoral edema, and total tumor volume. Other displayed measurements and classifications are derived in the workstation.
- The mobile app displays GIF previews created by desktop; it does not decode raw NIfTI or DICOM files or run segmentation.
- Cloud upload, collaboration, mobile sync, and AI features require their respective services and connectivity. Local desktop workflows and local history are available independently of those cloud features, subject to configuration.
- Account setup and login are role-specific for clinician and radiologist users.

## 4. Use case overview

| ID | Use case | Primary actor |
| --- | --- | --- |
| UC-01 | Sign in and initialize a workstation account | Clinician or radiologist |
| UC-02 | Upload a scan folder to the cloud | Radiologist |
| UC-03 | Import a patient case and run tumor segmentation | Clinician |
| UC-04 | Review and compare saved patient analyses | Clinician |
| UC-05 | Export a clinical report or analysis artifacts | Clinician |
| UC-06 | Sync segmentation previews for mobile access | Clinician |
| UC-07 | View a prepared case on mobile | Mobile reviewer |
| UC-08 | Ask the AI assistant about a case | Clinician |
| UC-09 | Collaborate through the desktop chat room | Clinician |

## 5. Detailed use cases

### UC-01 — Sign in and initialize a workstation account

**Goal:** Enter the correct NeuroQuant workspace with an authenticated role.

**Primary actor:** Clinician or radiologist.

**Preconditions:** NeuroQuant is installed and its local account database is available.

**Trigger:** The user starts NeuroQuant.

**Main flow:**

1. NeuroQuant opens the sign-in dialog before displaying the workspace.
2. The user selects the Neurologist or Radiologist role.
3. The user enters a username and password.
4. NeuroQuant validates the credentials for the selected role.
5. On first use of the default account, NeuroQuant prompts the user to set account credentials and profile information.
6. NeuroQuant opens the clinician workstation or radiologist upload workspace for the authenticated role.

**Alternative and exception flows:**

- If credentials are invalid, NeuroQuant displays an access error and allows another attempt.
- If the user exits or cancels authentication, the application closes.
- Clinician account recovery may restore an existing clinician account and data from the cloud when available.

**Postconditions:** The user is authenticated in the workspace associated with the chosen role.

### UC-02 — Upload a scan folder to the cloud

**Goal:** Make MRI or CT scan files available in cloud storage for later clinician import.

**Primary actor:** Radiologist.

**Supporting actors:** NeuroQuant cloud storage.

**Preconditions:** The radiologist is signed in and the cloud service is reachable.

**Trigger:** The radiologist chooses to upload a folder or selected files.

**Main flow:**

1. The radiologist selects **Upload Scan Folder** or **Upload Files**.
2. The radiologist chooses the local scan folder or files.
3. NeuroQuant uploads the files under the radiologist's cloud path and shows progress.
4. NeuroQuant displays uploaded scans in the workspace list.
5. The radiologist can refresh the list to retrieve the latest cloud contents.

**Alternative and exception flows:**

- If the network or cloud service is unavailable, NeuroQuant reports the upload or refresh failure.
- If the radiologist selects existing uploads and confirms deletion, NeuroQuant removes those cloud files.

**Postconditions:** Successfully uploaded scan files are available in the radiologist's cloud storage area.

### UC-03 — Import a patient case and run tumor segmentation

**Goal:** Produce an automated tumor mask and quantitative findings for clinician review.

**Primary actor:** Clinician.

**Supporting actors:** NeuroQuant desktop application and segmentation model.

**Preconditions:** The clinician is signed in. A supported case folder or cloud-uploaded case is accessible. The case contains the MRI sequences required by the loader.

**Trigger:** The clinician selects local scan import or imports a case from the cloud.

**Main flow:**

1. The clinician selects a scan folder or chooses a case from the cloud import list.
2. NeuroQuant requests and validates patient and attending physician details.
3. NeuroQuant saves or updates the patient record and loads FLAIR, T1, T1ce, and T2 MRI data. It also loads an optional CT scan when available.
4. NeuroQuant displays the loaded case in the multiplanar and 3D viewers.
5. The clinician reviews patient details and source images, then chooses **SCAN**.
6. NeuroQuant runs the segmentation model and displays progress.
7. NeuroQuant presents the segmentation overlay, 3D view, tumor core, edema, total volume, lobar measurements, and derived diagnostic summary.
8. NeuroQuant saves the analysis in local history and prepares preview GIFs.
9. The clinician reviews the original scans and generated findings.

**Alternative and exception flows:**

- If the clinician cancels required patient intake, the case is not loaded for analysis.
- If required MRI data cannot be read, NeuroQuant reports an import error.
- If model inference fails, NeuroQuant reports the error and does not present the run as complete.
- If an optional CT scan is absent, MRI review and segmentation can continue.

**Postconditions:** When successful, the case, segmentation, measurements, and history are available in the workstation for review and export.

### UC-04 — Review and compare saved patient analyses

**Goal:** Retrieve patient history and review changes across saved analyses.

**Primary actor:** Clinician.

**Preconditions:** The clinician is signed in and patient or segmentation history exists in the local database.

**Trigger:** The clinician opens the patient database, history panel, patient profiles, or longitudinal comparison tool.

**Main flow:**

1. The clinician searches or selects a patient or prior analysis.
2. NeuroQuant displays available patient details and saved analysis history.
3. The clinician opens the longitudinal comparison tool for the active patient and available metrics.
4. NeuroQuant presents the stored measurements for review.

**Alternative and exception flows:**

- If no matching records exist, NeuroQuant indicates that no records were found.
- If there is no prior analysis to compare, the clinician can review the current analysis without a comparison.

**Postconditions:** The clinician has reviewed the selected stored patient and analysis information.

### UC-05 — Export a clinical report or analysis artifacts

**Goal:** Save review materials for clinical documentation or sharing.

**Primary actor:** Clinician.

**Preconditions:** A segmentation has completed for the active case. Specific exports require computed results.

**Trigger:** The clinician selects an export action.

**Main flow:**

1. The clinician selects one of the available export formats.
2. NeuroQuant prompts for a destination file or folder.
3. NeuroQuant creates the requested artifact and logs the export in report history when supported.
4. NeuroQuant confirms the saved location.

**Available outputs:**

- Full clinical report as PDF.
- 3D render images as PNG.
- Orthogonal 2D slice images as PNG.
- Metrics and volumetrics as CSV.
- Diagnostic package as ZIP.

**Alternative and exception flows:**

- If segmentation or metrics are not available, NeuroQuant asks the clinician to run analysis first.
- If the destination cannot be written, NeuroQuant reports an export error.

**Postconditions:** The selected report or artifact is saved at the chosen destination.

### UC-06 — Sync segmentation previews for mobile access

**Goal:** Securely upload desktop-generated image previews for mobile review.

**Primary actor:** Clinician.

**Supporting actors:** NeuroQuant cloud services.

**Preconditions:** A segmentation has completed; the user has configured a mobile folder password; cloud services are reachable. Account verification may be required.

**Trigger:** NeuroQuant finishes preparing segmentation GIF previews.

**Main flow:**

1. NeuroQuant saves preview GIFs locally and registers the artifacts.
2. NeuroQuant checks whether a mobile folder password has been configured.
3. If needed, the clinician sets a folder password in desktop settings and verifies the cloud account.
4. NeuroQuant uploads the prepared previews to private cloud storage and records their folder metadata.
5. NeuroQuant reports the sync outcome.

**Alternative and exception flows:**

- If no mobile folder password is set, previews remain saved locally and the user is prompted to configure one before cloud sync.
- If account verification is cancelled, previews remain local.
- If some uploads fail, NeuroQuant reports the incomplete sync; successful files may still be available.

**Postconditions:** Synced previews can be listed and unlocked through the mobile app using the folder password.

### UC-07 — View a prepared case on mobile

**Goal:** Review synced image previews on a mobile device.

**Primary actor:** Mobile reviewer.

**Supporting actors:** NeuroQuant mobile app and cloud folder service.

**Preconditions:** Desktop has synced a case's GIF previews and a folder password has been set.

**Trigger:** The reviewer opens NeuroQuant Mobile.

**Main flow:**

1. The app lists available synced case folders.
2. The reviewer enters the folder password and opens a case.
3. The reviewer views prepared segmentation or original scan GIFs.
4. The reviewer selects axial, coronal, or sagittal view; MRI sequence; and play or pause.
5. The reviewer may choose CT view, which displays the raw CT preview without an MRI tumor overlay.
6. The reviewer locks the folder when finished.

**Alternative and exception flows:**

- If the folder password is incorrect, the app reports that access was denied.
- If a specific preview was not synced, the app indicates that the preview is unavailable.
- If no cases appear or the cloud service cannot be reached, the app displays an error and allows refresh.

**Postconditions:** The reviewer has viewed the available GIF previews. Raw DICOM/NIfTI data and segmentation inference remain outside the mobile app.

### UC-08 — Ask the AI assistant about a case

**Goal:** Get informational AI assistance related to brain tumor imaging or the selected patient context.

**Primary actor:** Clinician.

**Supporting actor:** AI assistant service.

**Preconditions:** The clinician is signed in. An AI API key and network connection are available for cloud-backed responses.

**Trigger:** The clinician opens the AI panel and submits a question or prompt.

**Main flow:**

1. The clinician opens the AI panel and enters a question.
2. When desired, the clinician attaches available patient or analysis context.
3. NeuroQuant sends the request to the configured AI service.
4. The service returns a response, which NeuroQuant displays in the chat panel.
5. The clinician independently reviews the response and underlying imaging findings.

**Alternative and exception flows:**

- If no API key is configured or the service is unavailable, the request cannot complete and NeuroQuant reports the issue.
- AI output is informational and does not replace clinician interpretation.

**Postconditions:** The response is available in the AI conversation, subject to the configured chat history behavior.

### UC-09 — Collaborate through the desktop chat room

**Goal:** Exchange messages with colleagues from the workstation.

**Primary actor:** Clinician.

**Supporting actors:** Other chat room users and the cloud chat service.

**Preconditions:** The clinician is signed in and network chat service is available.

**Trigger:** The clinician opens **Chat Room**.

**Main flow:**

1. NeuroQuant opens the collaboration panel.
2. The clinician views incoming messages and notifications.
3. The clinician writes and sends a message.
4. Other connected users can receive the message through the chat service.

**Alternative and exception flows:**

- If the chat service is offline, live collaboration and notifications may be unavailable.

**Postconditions:** The message is submitted to the chat service when connectivity is available.

## 6. Key nonfunctional expectations

- **Access control:** Require authentication before entering a role-specific desktop workspace; mobile preview folders require their configured password.
- **Progress feedback:** Show progress during scan import, segmentation, and cloud uploads.
- **Data reviewability:** Present the source images alongside segmentation overlays and quantitative outputs so clinicians can review results.
- **Cloud dependency awareness:** Surface connectivity or service errors for cloud-backed workflows.
- **Clinical oversight:** Clinicians remain responsible for interpreting AI-generated segmentation, measurements, and informational AI responses.

