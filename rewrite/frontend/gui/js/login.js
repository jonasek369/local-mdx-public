document.addEventListener("DOMContentLoaded", function () {
    fetch("/auth/check").then(response => response.json()).then(data => {
        const login_button = document.getElementById("login-button")
        if (data.status === "ok") {
            login_button.innerText = "Logged in"
        } else {
            login_button.innerText = "Missing credentials"
        }
    })
});