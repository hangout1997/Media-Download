# Media Download

這是一個基於 Streamlit 開發的線上影片與音訊批次下載工具，專為 macOS 與雲端硬碟（如 Google Drive）同步所設計。

## 🏗️ 系統設計準則 (Design Guidelines)

### 1. 記憶體優先 (RAM-First Pipeline)
- **核心規則**：盡量利用系統記憶體 (RAM) 處理資料串流，**嚴格避免**對實體硬碟 (SSD) 或雲端掛載磁區產生頻繁的讀寫與碎片化存取。
- **實作規範**：
  - 呼叫外部工具（如 FFmpeg）時，禁止使用暫存檔 (`tempfile` 或直接落地)。
  - 應全面採用標準輸出/輸入流 (例如 FFmpeg 的 `pipe:1`)。
  - 透過 `subprocess.PIPE` 將所有的二進位媒體資料先捕捉至 Python 的記憶體中。
  - 等待整個檔案資料在記憶體中完備後，再執行「**單次寫入 (Single Write)**」將資料一次性寫入最終目的地。
- **設計目的**：大幅減少硬碟磨損，並避免雲端同步軟體 (Google Drive File Stream) 因頻繁的檔案變更而觸發大量的網路 I/O 與 CPU 負載。
