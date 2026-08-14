#!/bin/bash
# Устанавливаем зависимости Python
pip install -r requirements.txt

# Устанавливаем системные зависимости для Playwright
playwright install-deps

# Устанавливаем браузер Chromium для Playwright
playwright install chromium

echo "✅ Playwright и браузеры установлены"
