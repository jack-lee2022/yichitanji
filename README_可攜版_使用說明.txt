================================================================================
  StockWarden 期貨/選擇權股票爬蟲 - 可攜版（隨身碟/任意資料夾皆可執行）
================================================================================

【這個目錄可以整包複製到任何電腦 / 隨身碟，不需重新安裝 Python 以外的東西】

  ▌ 攜帶內容（這個目錄全部檔案）
    ├ stockwarden_futures_options_scraper.py   主程式（唯一程式碼檔）
    ├ run.bat                                 一鍵執行（按兩下就好）
    ├ requirements.txt                        依賴套件聲明（目前全用標準函式庫）
    ├ notification_settings.example.json      LINE/Telegram 通知設定範本
    ├ taifex_list_cache.json                  期交所股票清單快取（第一次執行會自動建立）
    └ output/
        └ stockwarden_futures_or_options_stocks.html   輸出儀表板（執行後產生）

  ▌ 其他電腦（新電腦）第一次使用，三步驟：
    1. 安裝 Python 3.10 以上（推薦 3.12 或 3.14）
       → 下載：https://www.python.org/downloads/
       → ⚠️ 安裝畫面最下面「Add Python to PATH」一定要勾，否則 run.bat 找不到 python
    2. 把本目錄整包放到新電腦任何位置（C:\、D:\、桌面、隨身碟都行）
    3. 對準 run.bat 按兩下 → 自動爬最新資料 → 結束後 output/ 會有 HTML

  ▌ 選擇性功能：LINE / Telegram 通知
    1. 複製 notification_settings.example.json → 改名為 notification_settings.json
    2. 用記事本打開，填入你的 Bot Token / Chat ID 或 LINE Token / User ID
    3. 把 enabled 改為 true，儲存後重新執行 run.bat 就會自動推播

  ▌ 輸出檔案在哪？
    永遠會寫在「這個目錄底下的 output\stockwarden_futures_or_options_stocks.html」
    跟隨整包目錄移動，不會寫到其他位置。

  ▌ 排程自動執行（進階）
    Windows 工作排程器 → 建立基本工作 → 觸發程序選「每日」→
    動作選「啟動程式」→ 程式或指令碼 選這個目錄的 run.bat 即可。

================================================================================
  版本對應：stockwarden_futures_options_scraper.py（2026-10-02 版面升級版）
================================================================================
