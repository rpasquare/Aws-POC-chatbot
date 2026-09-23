# Deal Workspace — Requirements Document

**Status:** Draft for review
**Prepared for:** Project sign-off before development begins
**Version:** 1.0

---

## 1. Purpose of this document

This document records what will be built, what will not be built, and what still needs a decision. It is intended to be reviewed and approved before development starts.

Approving this document means agreeing to three things:

1. The behaviour described in sections 4 to 7 is what the application should do.
2. The items listed in section 9 are outside the scope of this project and would be treated as new work.
3. The open items in section 11 will be answered, and answers that arrive late may affect the delivery dates in section 12.

If something is missing, this is the point to say so. Changes after approval are possible but will move the dates.

---

## 2. What we are building, in plain terms

An internal web application where a team works through a set of PDF documents together.

A user uploads documents into a project. Any team member on that project can then ask questions in normal English and receive an answer built only from the content of those documents. Every answer carries references back to the exact page, and clicking a reference shows that page with the relevant passage highlighted.

Two problems this solves:

**Repeated work.** Conversations are saved against the project and visible to the whole team, so nobody re-asks a question a colleague already answered.

**Unverifiable answers.** Every figure in an answer can be traced to a page in a document in one click. Without that, nobody would act on what the system tells them.

The system answers only from the documents provided. If the documents do not contain the answer, it says so rather than estimating.

---

## 3. Who uses it

Access is granted per project. The same person can hold different roles on different projects.

| Action | Super admin | Admin | Editor | Viewer |
|---|---|---|---|---|
| Create a project | Yes | No | No | No |
| Assign a project admin | Yes | No | No | No |
| See every project in the system | Yes | No | No | No |
| Add members and set their roles | Yes | Yes | No | No |
| Upload documents | Yes | Yes | No | No |
| Delete and restore documents | Yes | Yes | No | No |
| Start a conversation | Yes | Yes | Yes | No |
| Ask a question | Yes | Yes | Yes | No |
| Read all conversations on the project | Yes | Yes | Yes | Yes |
| Open a cited page image | Yes | Yes | Yes | See open item 11.1 |

Notes on this table:

- A user sees only the projects they hold a role on. Other projects do not appear in their list and cannot be opened even with a direct link.
- Super admin is a system-wide role. It exists so that a manager can be appointed as admin of their own project.
- There are no per-person exceptions. Permissions come from the role only.
- All conversations on a project are visible to all members of that project immediately. There is no private draft and no sharing step.

---

## 4. End to end journey

This is the intended experience, in order.

**Sign in.** The user signs in and sees the projects they belong to.

**Open a project.** They see the project's documents and the conversations the team has already had.

**Add a document.** An admin adds a PDF. It appears in the list immediately, marked as processing. The upload itself is quick even for large files, because the file goes directly to storage rather than through the application server.

**Wait briefly.** The system reads the document in the background: text, tables, page layout, and the position of every passage on the page. A typical document becomes ready within a minute. The user does not have to wait on the screen, and the document cannot be used in answers until it is ready.

**Ask a question.** An admin or editor types a question. Within a few seconds an answer begins appearing, built only from passages found in that project's documents. Numbered references appear against the facts, and the source documents and pages are listed beneath.

**Verify an answer.** The user clicks a reference. The page appears as an image with the cited passage outlined. They confirm the figure is real and close the panel.

**Ask something not covered.** If the documents do not contain the answer, the system says so plainly and offers no reference. It does not estimate.

**Colleagues catch up.** Another team member opens the project later and reads the whole conversation, with the same clickable references.

**Replace a document.** When a document is superseded, an admin uploads the new version and deletes the old one. The deleted document stops appearing in any new answer immediately. It remains restorable for 30 days in case the wrong file was removed. Existing conversations that cited it still display, with those references marked unavailable.

---

## 5. Functional requirements

Each requirement below is written as a user story followed by acceptance criteria. The acceptance criteria are the testable contract.

### Requirement 1: Authentication

**User Story:** As a team member, I want to sign in to the application, so that my identity and permissions are known.

#### Acceptance Criteria

1. WHEN an unauthenticated visitor requests any application page other than the sign-in page THEN the system SHALL redirect them to sign in.
2. WHEN a user submits valid credentials THEN the system SHALL establish an authenticated session and show the user their project list.
3. WHEN a user submits invalid credentials THEN the system SHALL reject the attempt without revealing whether the account exists.
4. WHEN a user requests a password reset THEN the system SHALL send a reset link to the registered email address.
5. WHEN a user's session expires THEN the system SHALL require re-authentication before any further action.
6. WHEN any API request arrives without a valid session THEN the system SHALL reject it with an authentication error regardless of the request contents.

### Requirement 2: Project access and isolation

**User Story:** As a team member, I want to see only the projects I have been added to, so that work I am not part of remains separate.

#### Acceptance Criteria

1. WHEN a user views their project list THEN the system SHALL display only projects in which that user holds a role.
2. WHEN a user requests a project they hold no role in THEN the system SHALL respond as though the project does not exist.
3. WHEN a user is given a direct link to a project they hold no role in THEN the system SHALL deny access.
4. WHEN a search is performed THEN the system SHALL restrict results to chunks belonging to the one project the request is scoped to.
5. WHEN a request would return data from a project the user holds no role in THEN the database SHALL prevent the rows from being returned, independently of application code.
6. WHEN a user holds different roles on different projects THEN the system SHALL apply each project's role independently.

### Requirement 3: Role-based permissions

**User Story:** As a project admin, I want each team member's abilities to match their role, so that documents and conversations are managed by the right people.

#### Acceptance Criteria

1. WHEN a user holds the admin role on a project THEN the system SHALL permit them to upload documents, delete documents, restore documents, add members, set member roles, start conversations and ask questions.
2. WHEN a user holds the editor role on a project THEN the system SHALL permit them to start conversations, ask questions and read all conversations, and SHALL deny document upload, document deletion and member management.
3. WHEN a user holds the viewer role on a project THEN the system SHALL permit them to read all conversations, and SHALL deny asking questions, starting conversations, document upload, document deletion and member management.
4. WHEN a user attempts an action their role does not permit THEN the system SHALL reject the request at the server regardless of whether the client offered the control.
5. WHEN the client renders the interface THEN the system SHALL hide or disable controls the user's role does not permit, and SHALL explain why when a disabled control is activated.
6. WHEN permissions are evaluated THEN the system SHALL derive them from the user's role on the project only, without support for per-user exceptions.

### Requirement 4: Super admin and admin assignment

**User Story:** As a super admin, I want to create projects and appoint an admin for each, so that project ownership sits with the right manager.

#### Acceptance Criteria

1. WHEN a super admin creates a project THEN the system SHALL allow them to assign a user as the project admin.
2. WHEN a super admin accesses any project THEN the system SHALL grant them the full set of admin abilities on that project.
3. WHEN a super admin views the project list THEN the system SHALL display all projects in the system.
4. WHEN a user who is not a super admin attempts to create a project THEN the system SHALL deny the request.
5. WHEN a super admin changes a project's admin THEN the system SHALL apply the new permissions immediately to subsequent requests.

### Requirement 5: Document upload

**User Story:** As a project admin, I want to add PDF documents to a project, so that the team can ask questions about them.

#### Acceptance Criteria

1. WHEN a project admin initiates an upload THEN the system SHALL verify their role before permitting the upload to begin.
2. WHEN an upload is authorised THEN the system SHALL issue a time-limited credential that permits writing exactly one file to one location, and the file SHALL be transferred directly to object storage without passing through the application server.
3. WHEN the upload credential expires THEN the system SHALL reject any further use of it.
4. WHEN a file is offered that is not a PDF THEN the system SHALL reject it and state which formats are accepted.
5. WHEN a file exceeds the configured maximum size THEN the system SHALL reject it and state the limit.
6. WHEN an upload completes THEN the system SHALL record the document and enqueue its processing work as a single atomic operation, such that neither can exist without the other.
7. IF an upload is authorised but never completed THEN the system SHALL leave the document in a non-visible pending state and SHALL not present it as available.
8. WHEN a file identical in content to an existing document in the same project is uploaded THEN the system SHALL detect the duplicate and SHALL not process it a second time.

### Requirement 6: Document processing

**User Story:** As a project member, I want to see when a document is ready to be queried, so that I know whether answers can draw on it.

#### Acceptance Criteria

1. WHEN a document's processing work is enqueued THEN the system SHALL perform it asynchronously without blocking any user request.
2. WHEN a document is being processed THEN the system SHALL expose a status that distinguishes pending, parsing, indexing, ready and failed.
3. WHEN a document has not reached ready status THEN the system SHALL exclude its content from all search results.
4. WHEN a page contains an extractable text layer THEN the system SHALL extract its text without using optical character recognition.
5. WHEN a page contains no extractable text layer THEN the system SHALL apply optical character recognition to that page.
6. WHEN a document is parsed THEN the system SHALL record, for every extracted passage, its page number and its position on that page.
7. WHEN a document is parsed THEN the system SHALL produce and retain a page image for every page.
8. WHEN a document is parsed THEN the system SHALL retain the structured parse output, so that chunking and embedding can be repeated without reprocessing the original file.
9. WHEN processing fails THEN the system SHALL retry a bounded number of times before marking the document failed.
10. WHEN a document is marked failed THEN the system SHALL display that state with a reason and SHALL offer the admin a way to retry.
11. IF a processing worker stops unexpectedly while holding work THEN the system SHALL return that work to the queue after a timeout so it is not lost.
12. WHEN processing is repeated for a document THEN the system SHALL produce the same result as a first run, without duplicated content.

### Requirement 7: Grounded answers

**User Story:** As a project member, I want answers drawn only from my project's documents, so that I can rely on what I am told.

#### Acceptance Criteria

1. WHEN a user with permission to ask questions submits a question THEN the system SHALL retrieve candidate passages from ready documents in that project only.
2. WHEN passages are retrieved THEN the system SHALL construct the answer using only the content of those passages.
3. WHEN no retrieved passage meets the relevance threshold THEN the system SHALL state that the project's documents do not cover the question, and SHALL not generate a speculative answer.
4. WHEN the system states that the documents do not cover a question THEN it SHALL not present any citation.
5. WHEN a question is answered THEN the system SHALL begin returning the answer progressively rather than only on completion.
6. WHEN retrieval is performed THEN the system SHALL combine semantic similarity with exact term matching, so that identifiers such as section references are found reliably.
7. WHEN a question is embedded for retrieval THEN the system SHALL use the same embedding model that produced the stored chunk embeddings.

### Requirement 8: Citations and traceability

**User Story:** As a project member, I want to see exactly where an answer came from, so that I can verify a figure before acting on it.

#### Acceptance Criteria

1. WHEN an answer asserts information drawn from a passage THEN the system SHALL attach a numbered citation to that assertion.
2. WHEN an answer is stored THEN the system SHALL record, for each citation marker, the specific chunk it refers to.
3. WHEN a citation is displayed THEN the system SHALL show the source document name and page number.
4. WHEN a user with permission activates a citation THEN the system SHALL display the image of the cited page with the cited region visibly marked.
5. WHEN a citation's region is marked THEN the position SHALL remain correct at any display size.
6. WHEN a conversation is reopened later THEN the system SHALL resolve its citations to the same chunks, pages and regions as when the answer was produced.
7. WHEN a single answer draws on passages from more than one document THEN the system SHALL cite each source document separately.
8. WHEN the answer generation step produces citation markers THEN the system SHALL derive document, page and position from stored chunk data rather than from the generated text.
9. WHEN a citation refers to a document that is no longer available THEN the system SHALL indicate that the source is unavailable rather than failing to display the answer.

### Requirement 9: Conversations

**User Story:** As a project member, I want conversations kept with the project and visible to the team, so that nobody repeats analysis that has already been done.

#### Acceptance Criteria

1. WHEN a user with permission starts a conversation THEN the system SHALL associate it with the current project.
2. WHEN a conversation exists THEN the system SHALL make it readable to every member of that project, including viewers, without any sharing step.
3. WHEN a conversation is listed THEN the system SHALL show who started it and when it was last active.
4. WHEN a user reopens a conversation THEN the system SHALL display its questions, answers and citations as previously recorded.
5. WHEN a question is asked THEN the system SHALL record which user asked it.
6. WHEN a conversation is displayed THEN the system SHALL state that answers draw only on the documents in the current project.

### Requirement 10: Document deletion and restore

**User Story:** As a project admin, I want to remove a document that is no longer correct, with a safety net in case I remove the wrong one.

#### Acceptance Criteria

1. WHEN a project admin deletes a document THEN the system SHALL mark it deleted rather than destroying it immediately.
2. WHEN a document is marked deleted THEN the system SHALL exclude it from all subsequent search results.
3. WHEN a document is marked deleted THEN the system SHALL retain it in a restorable state for 30 days.
4. WHEN a project admin restores a document within the retention window THEN the system SHALL return it to ready status and include it in search again, without requiring re-upload or reprocessing.
5. WHEN the retention window elapses THEN the system SHALL permanently remove the document and its derived content.
6. WHEN a document is deleted THEN the system SHALL preserve existing conversations that cited it, marking those citations unavailable.
7. WHEN a user without the admin role attempts deletion or restore THEN the system SHALL deny the request.

### Requirement 11: Reprocessing

**User Story:** As an administrator, I want to rebuild a document's searchable content without re-uploading it, so that improvements can be applied to documents already in the system.

#### Acceptance Criteria

1. WHEN reprocessing of searchable content is requested for a document THEN the system SHALL rebuild chunks and embeddings from the retained parse output.
2. WHEN searchable content is rebuilt THEN the system SHALL not re-read the original file and SHALL not incur optical character recognition cost.
3. WHEN searchable content is rebuilt THEN the system SHALL replace the document's previous chunks rather than adding to them.
4. WHILE a document is being rebuilt THEN the system SHALL continue to serve existing conversations.

---

## 6. Operational and security requirements

### Requirement 12: Operational and security constraints

**User Story:** As the client, I want the system to protect our documents and behave predictably, so that it can be used on real work.

#### Acceptance Criteria

1. WHEN documents or derived files are stored THEN the system SHALL store them encrypted and SHALL block public access to the storage location.
2. WHEN a stored file is served to a user THEN the system SHALL do so through a time-limited credential rather than a durable public address.
3. WHEN data travels between the user and the system, or between system components THEN it SHALL be encrypted in transit.
4. WHEN resources are created THEN they SHALL reside in the client's agreed region.
5. WHEN an answer is requested THEN the system SHALL return the first content within a few seconds under normal load.
6. WHEN concurrent document processing exceeds capacity THEN the system SHALL queue the work and process it in order rather than failing.
7. WHEN processing capacity needs to increase THEN it SHALL be possible to add processing workers without changing application contracts.
8. WHEN the system records events THEN it SHALL log document lifecycle actions and permission denials sufficiently to investigate an incident.

---

## 7. Technology approach

Chosen deliberately for cost, maintainability and the ability to scale later without a rewrite.

| Layer | Choice | Why this, over the alternatives |
|---|---|---|
| Application | Python with FastAPI | Every mature PDF and AI library is Python-first. A different language would force a second service just for document parsing. |
| Database | PostgreSQL with the pgvector extension | Holds access control, document content, search data, conversations and the job queue in one place. Search and permission checks happen in the same query, so results cannot leak across projects through a forgotten filter. |
| Search | pgvector, combined with Postgres text search | A separate vector database would mean two systems to keep in sync and permission filtering done in application code. It would also add a monthly cost floor with no benefit at this scale. |
| File storage | Amazon S3 | Documents, parsed output and page images. Databases handle large binary files poorly. |
| PDF parsing | Docling, with Amazon Textract for scanned pages | Docling is MIT licensed, handles tables and figures, and provides the text positions needed for citation highlighting. Textract is used only for pages with no text layer, so OCR cost is incurred only where unavoidable. |
| AI models | Amazon Bedrock | Titan for converting text to searchable form, Claude for writing answers. Keeps documents inside the client's own AWS account and region, and billing inside the existing AWS relationship rather than requiring a new vendor. |
| Authentication | Amazon Cognito | Sign-in, sign-up, password reset and sessions. Building these is weeks of work and the area where a defect is most damaging. |
| Deployment | Containers on AWS, single database | Two deployable pieces: the web application and the background worker. Processing capacity scales by adding workers. |

### Structure of the system

Three parts, with deliberately separated responsibilities.

**Web application.** Handles sign-in, permissions, upload authorisation, search, answer generation and citation lookup. Never opens a PDF.

**Background worker.** Handles everything slow: reading documents, producing page images, preparing searchable content. Never serves a user request.

**Storage.** S3 for files. PostgreSQL for everything else, including the work queue.

The worker is separate because reading a large document takes up to a minute of sustained processing. If that happened inside a user request, the request would time out and the application would slow down for everyone.

### How a citation is guaranteed to be accurate

This is the most important design decision in the project, so it is stated explicitly.

The AI model that writes answers never produces a filename, page number or position. It receives passages labelled 1, 2, 3 and can only output those numbers. The application holds the mapping from each number to the specific passage it supplied, and each passage already carries its document, page and position, recorded when the document was parsed.

A citation therefore cannot point at a page that does not exist, or at a document from another project. That is a property of the design rather than something the model is trusted to get right.

One residual limitation, stated honestly: the model decides which passage a given fact came from, and can occasionally attribute a fact to the wrong passage among the ones it was given. All passages are genuine and all come from the correct project, so nothing is exposed that should not be. The mitigation is that a user can click the citation and see the page in about a second, which makes a wrong attribution visible rather than hidden. This is a significant reason the highlighting feature is worth building.

---

## 8. Cost expectation

Development phase, while nothing is deployed, is a few dollars a month. The database runs locally and only file storage and AI model calls are charged.

Running cost once deployed is driven by the database and compute instances rather than AI usage. AI cost is roughly two cents per question, so a thousand questions a month is around twenty dollars. Document parsing is close to zero for text-based PDFs, with optical character recognition charged only on scanned pages at roughly $1.50 per thousand pages.

A monthly budget alert has been configured at $50 for the development account, with warnings at $25 and $40.

Exact figures should be confirmed against current AWS pricing for the agreed region before any commitment is made to the client.

---

## 9. Explicitly out of scope

These are not included. Any of them would be new work with its own estimate.

**File formats.** Only PDF is supported. Excel, CSV, Word, PowerPoint, images and email files are not accepted.

**Calculations across spreadsheet data.** Not applicable while the system is PDF only, but noted because it is commonly assumed: the system does not sum, average or compare figures across rows of a table. It retrieves and quotes what the documents say.

**Answers from charts and diagrams.** Charts are treated as images. Their contents are not read and cannot be cited. See open item 11.2.

**Document editing.** No annotation, no redlining, no version chains, no comparison between two versions of a document.

**Per-user permission exceptions.** Permissions come from the four roles only.

**Private conversations.** Every conversation is visible to every member of the project. There is no draft or unshared state.

**Information barriers within a project.** It is not possible to place a member on a project but restrict them from part of it.

**Record of who read what.** The system logs actions such as upload and delete, but does not record which user read which answer. See open item 11.7.

**Export and reporting.** No export of conversations to Word, Excel or PDF. No usage dashboards or analytics.

**Integrations.** No connection to email, SharePoint, a document management system, or any external data source.

**Single sign-on with the client's identity provider.** Users are managed within the application's own directory.

**Mobile applications.** Desktop browser only. The interface is responsive but no native app is provided.

**Languages other than English.** Documents and questions are assumed to be in English.

**Offline use.** The application requires a network connection.

---

## 10. Assumptions

Development proceeds on these assumptions. If any is wrong, the dates in section 12 change.

1. Developer availability is 15 hours per week, and this is the binding constraint on delivery.
2. Documents are PDFs, and the majority are system-generated with a readable text layer rather than scans.
3. Documents are in English.
4. Document volume is in the order of tens to low hundreds per project, not tens of thousands.
5. Concurrent users are in the order of tens, not thousands.
6. The client has no requirement for the application to be publicly accessible on the internet beyond authenticated users.
7. Requirements do not change materially during the build. Changes are possible but are handled as scope changes with revised dates.
8. Sprint demonstrations can be reviewed within a few days of each sprint ending, so feedback does not block the next sprint.

---

## 11. Open items requiring a decision

These need answers. Where an answer is needed before a particular sprint, that is noted. Where no answer is given, the stated default will be built.

**11.1 Can a viewer open a cited page image?**
A viewer can read conversations and see that a citation refers to a named document and page. It has not been decided whether they can open the page image itself, or download the original file.
*Needed before sprint 6. Default if undecided: viewers can see the citation reference but cannot open the page image or the original file.*

**11.2 Do answers need to come from charts and diagrams?**
No document parser reads the meaning of a chart. Supporting this requires sending chart images to an AI model to describe them in words and indexing those descriptions. That is additional work and additional cost per document.
*Needed before sprint 2. Default if undecided: charts are not read, and questions answerable only from a chart will receive the "not covered" response.*

**11.3 How many of the client's documents are scans?**
This determines whether optical character recognition is a footnote or a significant cost and effort item, and affects the size of sprint 2.
*Needed before sprint 2. A sample of ten to twenty real documents would answer it definitively.*

**11.4 Which AWS region?**
All storage and databases must be created in the region the client requires, and this cannot be changed afterwards without migration and downtime. Model availability also varies by region and must be confirmed.
*Needed before sprint 1.*

**11.5 What are the file size and volume limits?**
Maximum file size per document, and expected number of documents per project. These set the upload limits and infrastructure sizing.
*Needed before sprint 1. Proposed default: 100 MB per file.*

**11.6 What are the client's availability and backup expectations, and is a security attestation such as SOC 2 required?**
These affect infrastructure choices, cost, and whether additional hardening work is needed.
*Needed before sprint 8, but earlier is considerably cheaper.*

**11.7 Is a record of who read which analysis required?**
If the client needs to evidence how information was handled, a read-level audit trail is needed. This is not currently in scope.
*Needed before sprint 5.*

**11.8 Is the delivery target the working application or a client-ready production system?**
These are different. See section 12.
*Needed now.*

---

## 12. Delivery plan

Two-week sprints at 15 hours per week. Each sprint ends with working software that can be demonstrated, not a status report. If a sprint runs short of time, the scope of that sprint reduces and the demonstration still happens.

| Sprint | Weeks | Demonstrable outcome |
|---|---|---|
| 1 | 1–2 | Upload a PDF and watch it become ready. The extracted text can be seen. |
| 2 | 3–4 | Ask a question and receive an answer citing document and page. Ask an unrelated question and receive the honest "not covered" reply. |
| 3 | 5–6 | Two projects side by side. The same question in each returns answers from that project's own documents only. |
| 4 | 7–8 | Sign in as admin, editor and viewer, and see the differences in what each can do. |
| 5 | 9–10 | Conversations persist across sessions and are visible to colleagues on the project. |
| 6 | 11–12 | Clicking a citation shows the page image with the cited passage highlighted. |
| 7 | 13–14 | Delete a document and see it excluded from answers. Restore it and see it return. |
| 8 | 15–16 | Running on AWS at a real address, used by the team with their own documents. |

### Two delivery targets

**Working application: 16 weeks.** Everything in this document, running and usable.

**Client-ready production system: 21 to 23 weeks.** Adds tested backups and restore, encryption verification, monitoring and alerting, a deployment pipeline, secrets management, rate limiting, and database resilience. These are not optional for a system a client depends on, and they are not included in the 16 weeks.

Open item 11.8 asks which of these is the commitment.

### What is committed and what is forecast

Sprint 2, ending week 4, is a firm commitment. It is close enough to estimate reliably and it is the sprint that proves the concept works.

Sprints 3 onwards are forecasts. They will be confirmed at the end of sprint 2, once actual delivery pace is known rather than estimated.

---

## 13. Risks

**Real documents differ from test documents.** The largest risk in the project. Document parsing quality can only be assessed against the client's actual files. Mitigation: obtain a sample of ten to twenty real documents before sprint 2, and treat sprint 2 as the decision point on answer quality.

**Answer quality may not meet expectations.** Retrieval and answer construction need tuning against real questions, and this is judgement work that cannot be shortened. Mitigation: the sprint 2 demonstration surfaces this in month one rather than month four.

**Authentication takes longer than estimated.** Consistently underestimated, and this is the developer's first backend project. Mitigation: sprint 4 is scoped to authentication alone, with no other deliverable competing for the time.

**Single developer at part-time availability.** There is no redundancy. Absence or reassignment stops progress entirely. Mitigation: all decisions are recorded in this document and the design document, so work can be handed over.

**Late answers to open items.** Several items in section 11 are needed before specific sprints. Late answers either stall a sprint or force rework. Mitigation: the sprint each item is needed by is stated.

**Scope growth.** The most common cause of missed dates. Mitigation: section 9 lists what is excluded, and additions are handled as scope changes with revised dates rather than absorbed silently.

---

## 14. Sign-off

Approval confirms that the behaviour in sections 4 to 7 is correct, the exclusions in section 9 are accepted, the assumptions in section 10 hold, and the open items in section 11 will be answered.

| Role | Name | Date | Approved |
|---|---|---|---|
| Project manager | | | |
| Developer | | | |

Development begins once this document is approved and open items 11.4, 11.5 and 11.8 are answered.
