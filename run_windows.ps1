$ErrorActionPreference = 'Stop'

if (-not (Test-Path '.venv')) {
    python -m venv .venv
}

.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt

if (-not (Test-Path '.env')) {
    Copy-Item .env.example .env
    Write-Host 'Created .env. Edit it with your Gmail/App Password/Drive settings, then run again.'
    exit
}

python app.py
