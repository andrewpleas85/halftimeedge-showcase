# Architecture

```mermaid
flowchart LR
    subgraph Sources
        A[Odds API<br/>lines and odds]
        B[ESPN scoreboards<br/>scores and status]
    end

    A --> C[Halftime loop<br/>capture and evaluate]
    B --> C
    C --> R[(Raw odds snapshots<br/>stored verbatim)]
    C --> S[Strategy registry<br/>+ conflict router]
    S --> D[Discord alerts<br/>tier + settled record]
    S --> DB[(SQLite<br/>source of truth)]
    DB -->|one way only| M[(DynamoDB mirror<br/>website reads)]

    B --> T[Nightly settler<br/>grades by game date]
    T --> DB
    R --> P[Price recovery<br/>real odds per bet]
    P --> DB

    DB --> W[Capture watchdog<br/>alerts on silent gaps]
    W --> D

    X[External picks service] --> E[Forward tracker<br/>store raw, grade independently]
    B --> E
```

## Reading the diagram

| Component | Guarantee it provides |
|---|---|
| Raw odds snapshots | Any parsing mistake can be corrected later by re-reading the original response |
| SQLite → DynamoDB | One direction only; the mirror can never overwrite the record |
| Nightly settler | A bet is graded against its own game date, not the date the job runs |
| Price recovery | Units reflect the odds actually available, not an assumed price |
| Capture watchdog | A quiet failure (HTTP 200 with empty data) becomes an alert |
| Forward tracker | A third party's claims are graded by us on games that hadn't happened when recorded |
