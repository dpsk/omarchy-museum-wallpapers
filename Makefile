PLUGIN_ID := io.github.dpsk.museum-wallpapers
PLUGIN_DIR := $(HOME)/.config/omarchy/plugins/$(PLUGIN_ID)
QMLLINT := $(firstword $(wildcard /usr/lib/qt6/bin/qmllint) $(shell command -v qmllint 2>/dev/null))

.PHONY: test lint validate check dev

test:
	python3 -m unittest discover -s test

lint:
	python3 -m py_compile museum.py
	@if [ -n "$(QMLLINT)" ]; then $(QMLLINT) Service.qml BarWidget.qml; else echo "qmllint not found, skipping"; fi

validate:
	omarchy-plugin-validate .

check: test lint validate

# Copy the working tree over an installed copy. The bar widget reloads on its
# own; the keepLoaded service needs `omarchy restart shell`.
dev:
	@test -d "$(PLUGIN_DIR)" || { echo "Install first: omarchy plugin add $(CURDIR) --enable"; exit 1; }
	rsync -a --delete --exclude .git --exclude __pycache__ ./ "$(PLUGIN_DIR)/"
