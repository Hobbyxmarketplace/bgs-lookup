const form = document.getElementById("search-form");
const idType = document.getElementById("id-type");
const idValue = document.getElementById("id-value");
const searchBtn = document.getElementById("search-btn");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");
const jobSummaryEl = document.getElementById("job-summary");
const itemsGridEl = document.getElementById("items-grid");
const rawJsonEl = document.getElementById("raw-json");

const JOB_FIELDS = [
  "job_status",
  "invoice_id",
  "job_id",
  "submission_id",
  "service_type_name",
  "service_term_name",
  "received_date",
  "deadline_date",
  "ship_date",
  "tracking_num",
  "number_of_cards_received",
  "number_of_cards_graded",
  "total_paid",
  "total_due",
];

function setStatus(message, kind) {
  if (!message) {
    statusEl.hidden = true;
    statusEl.textContent = "";
    statusEl.className = "status";
    return;
  }
  statusEl.hidden = false;
  statusEl.textContent = message;
  statusEl.className = kind ? `status ${kind}` : "status";
}

function formatValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

function renderJobSummary(job) {
  jobSummaryEl.innerHTML = "";
  if (!job) {
    jobSummaryEl.textContent = "No job details in response.";
    return;
  }

  const heading = document.createElement("h2");
  heading.textContent = job.set_name || `Order ${job.job_id ?? ""}`;
  jobSummaryEl.appendChild(heading);

  const grid = document.createElement("div");
  grid.className = "field-grid";

  for (const key of JOB_FIELDS) {
    if (!(key in job) || job[key] === null || job[key] === "") continue;
    const field = document.createElement("div");
    field.className = "field";

    const labelEl = document.createElement("span");
    labelEl.className = "field-label";
    labelEl.textContent = key;

    const valueEl = document.createElement("span");
    valueEl.className = "field-value";
    valueEl.textContent = job[key];

    field.appendChild(labelEl);
    field.appendChild(valueEl);
    grid.appendChild(field);
  }

  jobSummaryEl.appendChild(grid);
}

function renderImageCell(url) {
  const cell = document.createElement("td");

  const link = document.createElement("a");
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.className = "image-cell";

  const thumb = document.createElement("img");
  thumb.className = "cell-thumb";
  thumb.src = url;
  thumb.alt = "card image";
  thumb.loading = "lazy";
  thumb.onerror = () => {
    thumb.remove();
  };

  const text = document.createElement("span");
  text.textContent = url;

  link.appendChild(thumb);
  link.appendChild(text);
  cell.appendChild(link);
  return cell;
}

function renderItems(items) {
  itemsGridEl.innerHTML = "";
  if (!items || items.length === 0) return;

  for (const item of items) {
    const card = document.createElement("div");
    card.className = "item-card";

    const table = document.createElement("table");
    table.className = "item-table";
    const tbody = document.createElement("tbody");

    for (const [key, value] of Object.entries(item)) {
      const row = document.createElement("tr");

      const th = document.createElement("th");
      th.textContent = key;
      row.appendChild(th);

      if (key === "image_url" && value) {
        row.appendChild(renderImageCell(value));
      } else {
        const td = document.createElement("td");
        td.textContent = formatValue(value);
        row.appendChild(td);
      }

      tbody.appendChild(row);
    }

    table.appendChild(tbody);
    card.appendChild(table);
    itemsGridEl.appendChild(card);
  }
}

async function runSearch(type, value) {
  setStatus("Loading…", "loading");
  resultsEl.hidden = true;
  searchBtn.disabled = true;

  try {
    const url = `/api/lookup?${type}=${encodeURIComponent(value)}`;
    const resp = await fetch(url);
    const data = await resp.json();

    if (!resp.ok) {
      throw new Error(data.error || `Request failed (${resp.status})`);
    }

    const job = Array.isArray(data.job_details) ? data.job_details[0] : data.job_details;
    const items = data.job_items_listing_array || [];

    renderJobSummary(job);
    renderItems(items);
    rawJsonEl.textContent = JSON.stringify(data, null, 2);

    resultsEl.hidden = false;
    setStatus(null);
  } catch (err) {
    setStatus(err.message || String(err), "error");
  } finally {
    searchBtn.disabled = false;
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const value = idValue.value.trim();
  if (!value) return;
  runSearch(idType.value, value);
});
