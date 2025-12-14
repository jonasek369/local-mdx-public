# Local mangadex
Local mangadex is open source local website using mangadex API. With the ability to download manga to your local drive (currently maximum database capacity for local storage was tested up to 200GB) and use even offline

## Roadmap (based on priority)
- [ ] Fix recently updated page
- [x] Add more content to landing page
- [ ] Add support for long strip
- [ ] Add navbar/settings to landing page
- [ ] Add tests for making sure offline works
- [ ] Add pages to library 
- [ ] Add advanced Search page
- [ ] Add option to read manga without downloading it
- [ ] Remake the code folder structure

## Known issues
- [x] Redundant API Calls for Cover arts
- [ ] Downloader rate limit timeout sends extra request
- [ ] When adding too many mangas to downloader it crashes

## Downloader
the new downloader has been rewritten in C (mainly for fun, also it won't hang the site) you will need clone [this repo](https://github.com/jonasek369/C-manga-downloader) 
and compile it then move main.exe, libcurl-x64.dll, curl-ca-bundle.crt into /rewrite/frontend. Still in early development
if you find any bug please create an Issue