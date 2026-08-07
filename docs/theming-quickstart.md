# Source copy for the theme guide page

Everything from the next heading down is customer-facing copy for the single
self-contained HTML page served at the widget bucket root. Render it as HTML;
do not add anything that is not here, and do not reword it.

Written for the reader who has AWS console access, because every step needs the
console. Someone who only knows the desired colour needs a short email, not
this page. Headings are section boundaries, so a screenshot can be added under
any step later without restructuring.

Two conventions the page must keep. The download link is the relative path
`defaults/theme.json`, so the page never needs to know the distribution domain.
The two console links are always called by their output names,
`WidgetThemeDownload` and `WidgetThemeUpload`, and never described in other
words.

---

# Changing the chatbot's colours and questions

## What you can change

Three things: the main colour, the font, and the example questions the widget
offers before anyone has typed. Nothing else is adjustable.

## Before you start

You need sign-in access to the college's AWS account. Every step below happens
there.

If someone asked you to make this change, ask them for:

- the hex colour they want, for example `#1b3a6b`
- which font they want. The allowed fonts are listed inside the settings file
  you download in step 1, so send them that list if they need it.
- up to four suggested questions, per language

## Step 1: get the settings file

Download the settings file. It saves to your computer as `theme.json`.

Come back for a fresh copy any time. This link always gives you the original,
untouched. It is also in the Outputs tab as `WidgetThemeDownload`.

## Step 2: edit it

Open `theme.json` in a plain text editor. VS Code is ideal. Notepad works.

Do not use Microsoft Word, and do not use TextEdit in rich text mode. They
replace straight quotes with curly ones, and the file then silently stops
working.

The instructions are inside the file. It explains all three settings.

Save it, and keep the name exactly `theme.json`. If your editor adds `.txt`,
remove it.

## Step 3: upload it

In AWS, open CloudFormation, click the chatbot stack, and open the **Outputs**
tab. At Gavilan College that stack is named `GavilanChatbotStack`.

Click the link labelled `WidgetThemeUpload`. It opens the right storage
location.

Click **Upload**, drag `theme.json` in, and click **Upload** again. Accept every
default.

Keep that Outputs tab open. You will use it every time you make a change.

## Step 4: check it

Wait a full minute. The settings are cached for 60 seconds, so a refresh before
that still shows the old look.

Then hard-refresh the library page. That is Cmd Shift R on a Mac, Ctrl Shift R
on Windows.

## Changing something later

Edit your saved `theme.json`, then upload it again the same way. The same
filename replaces the old file. There is no delete step and no warning to
answer.

Keep your copy somewhere you will find it again. It is your working file.

## Going back to the default look

Open `WidgetThemeUpload` and delete `theme.json` from the storage location. The
widget goes back to the settings it shipped with.

## If something looks wrong

**Nothing changed at all.** Usually the cache. Wait the full minute and
hard-refresh again. If it still looks the same, check that the file in the
storage location is named exactly `theme.json`, with no `.txt` and no `(1)`.

**Everything went back to the default look.** The file is probably malformed. A
missing comma, a curly quote, or an extra comma after the last entry will do it.
The widget is built to ignore a broken settings file rather than break the page,
so nothing reports an error anywhere. Paste the contents into any online JSON
validator, fix what it reports, and upload again.

**A colour looks wrong rather than missing.** Text colour is not a setting. The
widget picks black or white on its own, whichever is easier to read against your
colour. A pale colour turns the text black.

**The page itself is broken.** That is not the theme. No settings file, however
bad, can affect the page the widget sits on. Raise it with DxHub.
