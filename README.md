# local-mdx

Lmdx is an offline use of mangadex. It is very early in its development

# How to use

before starting, make sure you check settings.json  
if they do not exist create them your self with this as basic template  

settings.json
```json
{
    "LogLevel": 1,
    "redisCaching": false,
    "OnStart": {
        "cacheMangas": false
    },
    "DownloadProcessor": {
        "defaultSpeedMode": "NO_LIMIT",
        "useThreading": true,
        "silentDownload": true,
        "saveQueueOnExit": true,
        "runOnStart": true,
        "haltOnRateLimitReach": true
    },
    "MangadexConnection": {
        "excludedGroups - comment": "Here add any official group because if MangadexConnection comes across official publication it most likely redirects to different website so the connection will fail",
        "excludedGroups": [
            "4f1de6a2-f0c5-4ac5-bce5-02c7dbb67deb"
        ],
        "cacheChapterInfoToDatabase": true
    },
    "AlertSystem": {
        "discordWebhook": "",
        "discordRecipient": -1,
        "soundAlertOnRelease": false,
        "downloadStartOnRelease": true,
        "watchedMangas": [

        ],
        "cooldown": 600
    },
    "DiscordIntegration": {
        "showPresence": true,
        "filter": [
            [
                "include", "!=", ["uuid-here"],
                "exclude", "==", ["some-uuid"]
            ]
        ]
    }
}
```

## How to start

make sure you have all dependencies installed  go to src/backend/main.py and start

## Termidex
Terminal based version of lmdx
