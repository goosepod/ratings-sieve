(() => {
  const form = document.getElementById("rs-settings-form");
  const accounts = document.getElementById("rs-accounts");
  const template = document.getElementById("rs-account-template");
  const add = document.getElementById("rs-add-account");
  const result = document.getElementById("rs-action-result");
  function addAccount() {
    if (accounts.querySelectorAll("[data-account]").length >= 50) return;
    const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, "0")).join("");
    const fragment = template.content.cloneNode(true);
    fragment.querySelectorAll("[name]").forEach(input => { input.name = input.name.replaceAll("__ID__", id); });
    fragment.querySelector("[name=profile_id]").value = id;
    accounts.appendChild(fragment);
    accounts.lastElementChild.querySelector("input:not([type=hidden])").focus();
  }
  add.addEventListener("click", addAccount);
  accounts.addEventListener("click", event => {
    const button = event.target.closest("[data-remove-account]");
    if (button) button.closest("[data-account]").remove();
  });
  form.addEventListener("submit", async event => {
    const action = event.submitter?.value || "save";
    if (action === "save") return;
    event.preventDefault();
    const data = new FormData(form);
    data.set("action", action);
    result.hidden = false;
    result.textContent = action === "test" ? "Testing connections…" : "Clearing cached ratings…";
    const buttons = form.querySelectorAll("button");
    buttons.forEach(button => { button.disabled = true; });
    try {
      const endpoint = form.getAttribute("action") || window.location.href;
      const response = await fetch(endpoint, {method: "POST", body: data, credentials: "same-origin", headers: {"X-Requested-With": "RatingsSieve"}});
      const payload = await response.json();
      result.textContent = payload.message || payload.error || "The action could not be completed. Refresh and try again.";
    } catch {
      result.textContent = "The action could not be completed. Refresh and try again.";
    } finally {
      buttons.forEach(button => { button.disabled = false; });
    }
  });
  if (!accounts.children.length) addAccount();
})();
