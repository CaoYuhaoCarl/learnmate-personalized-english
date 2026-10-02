.PHONY: pdf check-tools

DATE ?= $(shell date +%F)

pdf:
	python3 -m english_coach.pdf_export --date "$(DATE)"

check-tools:
	@command -v pandoc >/dev/null || { echo "Missing pandoc. Install it with: brew install pandoc"; exit 1; }
	@command -v wkhtmltopdf >/dev/null || { echo "Missing wkhtmltopdf. Install it from: https://wkhtmltopdf.org/downloads.html"; exit 1; }
	@echo "PDF export tools are installed."
