<div align="center">

# vedit

**A video editing pipeline that an AI assistant runs for you. You describe the edit in normal words, the assistant writes the decisions into one file, and the program renders the video from that file.**

![Python](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)
![ffmpeg](https://img.shields.io/badge/ffmpeg-8-007808?logo=ffmpeg&logoColor=white)
![Status](https://img.shields.io/badge/status-work%20in%20progress-E3B341)

</div>

## What is this?

Editing talking-head videos takes a lot of time: cutting out pauses and "ähm"s, adding captions, adding music at the right volume. I wanted to just say what I want and let an AI assistant do the work. vedit is the tool the assistant uses for that.

It takes a raw recording and turns it into a finished video with cuts, captions, graphics, music and the right loudness.

## The steps

```mermaid
flowchart LR
    A[Raw video] --> B[Transcript]
    B --> C[Cuts]
    C --> D[Captions and graphics]
    D --> E[Music and loudness]
    E --> F[Finished video]
```

1. **Intake:** the video is converted to a fixed frame rate.
2. **Transcript:** WhisperX writes down every word with its exact time.
3. **Story and cuts:** the assistant decides what stays. Pauses, filler words and repeated takes are removed.
4. **Graphics and captions:** titles are made from HTML pages, captions are burned into the video.
5. **Music:** background music gets quieter while someone talks, and the loudness is fixed.
6. **Check:** a final test looks for missing frames, wrong fonts, blank graphics and wrong loudness.

## Three ideas that made it work

- **Only whole frames.** If you cut at exact times from the transcript, the audio slowly drifts away from the picture. I measured more than one second of drift over 150 cuts. When every cut is on a whole frame, there is no drift at all.
- **The edit is a file.** Every decision is saved in project.json. Moving a cut by 200 ms means changing one number, and only that part of the video is rendered again.
- **Nothing gets deleted.** When a pause or a filler word is removed, it stays in the file, marked as turned off and with a reason. You can undo every decision.

## How to use it

Normally you just tell the assistant what you want, in German or English. You can also run the steps yourself:

```powershell
.\tools\setup.ps1
.\tools\ve.ps1 new ep042 --preset longform --platform youtube
.\tools\ve.ps1 intake ep042 "C:\Videos\raw\cam_a.MP4"
.\tools\ve.ps1 transcribe ep042 --language de
.\tools\ve.ps1 cut ep042
.\tools\ve.ps1 render ep042
```

## What is not finished

- The numbers for cutting (how long a pause has to be, and so on) come from research. I have not tested them on real recordings yet.
- The longest video I tested was 60 seconds long.
- The speech recognition setup is planned, but I have not run the full install yet.

## Folders

| Folder | Content |
|---|---|
| engine | The program itself, written in plain Python |
| tools | The scripts you start |
| presets | Settings for formats, platforms, captions and cuts |
| assets/fonts | The fonts for captions and graphics |
| projects | One folder per video. Only the template and one example are in this repository |
| docs | Detailed documentation of every step |
| .claude | The instructions and skills the AI assistant uses |

## License

MIT, see [LICENSE](LICENSE). The fonts in assets/fonts have their own open license, see [NOTICE](NOTICE).
