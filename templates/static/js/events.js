// HANDLE SELECT ALL FOR BULK ACTIONS
function toggleSelectAll(checkbox, tableId) {
    const checked = checkbox.checked;
    const table = document.getElementById(tableId);
    const rows = table.querySelectorAll("input.event-checkbox");
    rows.forEach(cb => cb.checked = checked);
}

// COLLPASE / EXPAND SECTIONS
function toggleSection(sectionId, iconId) {
    const section = document.getElementById(sectionId);
    const icon = document.getElementById(iconId);

    if (section.style.display === "none") {
        section.style.display = "block";
        icon.classList.add("open");
    } else {
        section.style.display = "none";
        icon.classList.remove("open");
    }
}

// FILTERING (COLUMN FILTERS)
function filterTable(tableId) {
    const table = document.getElementById(tableId);
    const filters = table.querySelectorAll(".filter-input");

    const rows = table.querySelectorAll("tbody tr");

    rows.forEach(row => {
        let visible = true;

        filters.forEach((filter, idx) => {
            const filterValue = filter.value.toLowerCase();
            const cell = row.children[idx + 1]; // skip checkbox column

            if (filterValue && !cell.innerText.toLowerCase().includes(filterValue)) {
                visible = false;
            }
        });

        row.style.display = visible ? "" : "none";
    });
}

// BULK ASSIGN: COLLECT SELECTED IDS
function collectBulkIds(tableId, hiddenInputId) {
    const table = document.getElementById(tableId);
    const checkboxes = table.querySelectorAll("input.event-checkbox:checked");
    const ids = [...checkboxes].map(cb => cb.value);
    document.getElementById(hiddenInputId).value = JSON.stringify(ids);
}
