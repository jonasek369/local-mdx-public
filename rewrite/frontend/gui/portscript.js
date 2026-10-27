// script designed to paste to console in mangadex library to get your library entries
var uuids = [];

document.getElementsByClassName("manga-card").forEach((element) => {
    var href_url = element.getElementsByClassName("font-bold title")[0].href;
    var splited = href_url.split("/")
    uuids.push(splited[4])
});
console.log(uuids);