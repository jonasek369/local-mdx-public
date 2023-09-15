# local-mdx

Lmdx is offline use mangadex. It is very early in its development

# How to use

before starting make sure you check settings.json  
if they do not exist create them your self with this as basic template  

settings.json
```json
{
    "LogLevel": 1,
    "DownloadProcessor": {
        "defaultSpeedMode": "NORMAL",
        "settings": {
            "silent_download": true
        }
    },
    "MangadexConnection": {
        "excludedGroups - comment": "Here add any official group because if MangadexConnection comes across official publication it most likely redirects to different website so the connection will fail",
        "excludedGroups": [
            "4f1de6a2-f0c5-4ac5-bce5-02c7dbb67deb"
        ],
        "cacheChapterInfoToDatabase": true
    },
    "AlertSystem": {
        "discordWebhook": "https://discord.com/api/webhooks/1234567890/your-webhook",
        "discordRecipient": 395165520229433344,
        "soundAlertOnRelease": true,
        "downloadStartOnRelease": false,
        "watchedMangas": [
            
        ],
        "cooldown": 600
    }
}
```


**MAKE SURE THESE FILES ARE IN THE SAME FOLDER AS EXE OR LAUNCHER**

## in case of source code

make sure you have all dependencies installed  
go to src/backend/main.py and start

## any crashes/errors report to me