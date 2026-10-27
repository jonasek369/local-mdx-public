# Local MangaDex

Local MangaDex is an open-source, self-hosted web application that uses the MangaDex API.  
It allows you to download manga to your local drive and read them offline.

## Installation & Running

1. Clone or download this repository.
2. Install Python 3.12 or newer.
   - Development and testing are done on Python 3.13.1
3. Run the appropriate startup script:
4. Go to localhost:5000

### Windows
```start.bat```

### Linux
```start.sh```


## Roadmap (Priority-Based)

- [x] Fix recently updated page
- [x] Add more content to landing page
- [x] Remove back buttons from pages and rely on browser navigation
- [x] Add support for long strip manga
- [ ] Add navbar and settings to landing page
- [ ] Add tests to ensure offline functionality
- [ ] Add pages to library view
- [ ] Add advanced search page
- [ ] Add option to read manga without downloading

## Known Issues

- [x] Redundant API calls for cover art
- [ ] Downloader rate-limit timeouts send extra requests
