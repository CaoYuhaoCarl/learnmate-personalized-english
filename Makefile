.PHONY: pdf check-tools

DATE ?= $(shell date +%F)

pdf:
	python3 -m english_coach.pdf_export --date "$(DATE)"

check-tools:
	@pandoc --version >/dev/null 2>&1 || { echo "pandoc is missing or cannot run. On macOS, install it with: brew install pandoc"; exit 1; }
	@weasyprint --version >/dev/null 2>&1 || { echo "WeasyPrint is missing or cannot run. On macOS, install it with: brew install weasyprint"; exit 1; }
	@echo "PDF export tools are installed and runnable."
