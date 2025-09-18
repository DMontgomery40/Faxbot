Project Plan: iOS 26 Update and Fax Share Sheet Integration
Overview & Objectives

Upgrade UI for iOS 26: Update the app’s design to align with Apple’s new Liquid Glass interface introduced in iOS 26. This includes supporting dynamic light/dark appearances (following the system setting) and ensuring the app’s visuals utilize iOS 26’s translucent Liquid Glass material for a modern look
apple.com
. The app should default to the system appearance (no hard-coded light or dark mode) so it automatically adapts to user preferences.

Share Sheet Fax Feature: Implement a new “Fax” action extension on iOS, allowing users to send documents via fax directly from the iOS Share Sheet. For example, a user who takes a photo of a document (e.g. an insurance card) can tap the share button and select “Fax” from the share sheet. They will then be prompted to choose a fax recipient (e.g. Dr. Henderson – Fax Number), and the app will send the fax in the background via our FaxBot service. This feature aims to streamline faxing by eliminating the need to open the app and manually attach files.

Seamless Background Sending: Ensure that fax transmissions occur in the background, providing a smooth user experience. Once a fax is initiated from the share sheet, the user shouldn’t have to keep the app open. The FaxBot integration will handle sending the fax behind the scenes and notify the user upon success or failure.

Maintain Performance & Constraints: Define reasonable limits for fax submissions based on typical use. Extremely large faxes (e.g. dozens of high-resolution pages) are not expected in normal use, so we will limit the initial scope to common sizes (for instance, up to ~10 images or a few PDF pages)
stackoverflow.com
. This avoids performance bottlenecks and memory issues. If future demand shows users need to send larger faxes, we will plan to extend support later.

By achieving these objectives, the app will feel up-to-date on iOS 26 and offer a highly convenient way for users to fax documents directly from Photos or other apps, meeting the user’s requirements (points 1, 3, 4, 5, 6 as discussed). Next, we detail each aspect of the plan.

UI/UX Update for iOS 26 (Liquid Glass Design)

Apple’s iOS 26 is a major redesign (the biggest visual overhaul since iOS 7), introducing a new unified design language across devices. Our app must be updated to match the iOS 26 look and feel, which will both improve aesthetics and ensure compatibility:

Adopt the Liquid Glass Material: iOS 26 introduces Liquid Glass, a translucent, dynamic material that gives interfaces a glass-like depth
apple.com
apple.com
. All standard UI components (navigation bars, tab bars, buttons, etc.) should use the system’s default materials so that they automatically get this new translucent effect. For example, navigation bars and tab bars will now have a blurred, glassy background that reacts to content behind them, instead of a solid color. We will remove any custom static backgrounds in these elements and use Apple’s new API for standard appearances
apple.com
. This ensures our app’s controls will dynamically morph and adapt just like native apps, bringing a “delightful” and modern feel to interactions
apple.com
.

Support Light & Dark Mode (System Default): The Liquid Glass UI intelligently adapts to both light and dark environments
apple.com
. We will make sure our color palette uses system colors (like label, background, etc.) so that text and icons automatically switch to high-contrast colors in dark mode. The app’s appearance will default to the system setting, meaning if the user’s device is in dark mode, the app will appear in dark mode, and vice versa (with no manual toggle needed). Any custom UI elements will be reviewed to ensure they also have dark-mode variants or use dynamic colors. For example, if we have a custom logo or splash screen, we might provide inverted colors for dark mode. By doing this, the app will blend seamlessly with iOS 26’s dynamic theming and “maintain the familiarity of Apple’s software”
apple.com
.

Rounded Corners & Modernized Controls: iOS 26 heavily emphasizes rounded corners and fluid shapes in the UI. We’ll audit our app for any outdated styles (e.g. overly square containers or legacy controls) and update them. Lists/table views should get the new grouped style with rounded section corners and a bit more spacing, which gives a friendlier, “less rigid” look
macstories.net
. Controls like toggles and sliders gain new visuals (e.g. sliders become pill-shaped and turn into a larger Liquid Glass bubble when interacted with)
macstories.net
. We will use the updated UIKit/SwiftUI components so these changes appear by default. No heavy custom UI drawing unless necessary – by using standard components, we get Apple’s new animations (like slider momentum and the bouncing loupe text magnifier) for free
macstories.net
macstories.net
.

Ensure Legibility and Contrast: One challenge with the new translucent designs is potential legibility issues. In fact, some users have criticized that Liquid Glass can make content illegible in certain cases
macrumors.com
. We will pay careful attention to text contrast and readability. For example, text on a blurred background must remain readable whether the underlying content is light or dark. Since Liquid Glass tints itself based on context, we’ll test our screens with various backgrounds. Apple provides guidance (and perhaps APIs) to ensure content stands out – e.g., using vibrancy or proper material effects. We’ll avoid placing low-contrast text on semi-transparent panels. During testing, if we find any screen where the background content bleeds through too much (making text hard to read), we’ll adjust by using a slightly more opaque material or a darker/lighter blur effect as needed. Our goal is to embrace the stylish new look without sacrificing usability.

Dynamic Icon and Tinting (if applicable): iOS 26 also introduced Liquid Glass app icons and the ability for icons and widgets to have light, dark, tinted, or clear variations
apple.com
. If time permits, we will update our app icon using Apple’s Icon Composer so that it can render beautifully in all modes
apple.com
. This is not a core functional requirement, but it’s a polish item: for example, the icon could have a translucent layer or subtle glow that matches iOS 26’s style. This ensures the app icon doesn’t look out of place on the new Liquid Glass home screen (where icons have subtle glowing corners and depth)
macrumors.com
. We will also verify that our share extension’s icon (which appears in the share sheet) looks correct in the new circular format (likely a monochromatic glyph on a rounded background in the share sheet favorites row – discussed more below).

Minimal Customization for Future-proofing: Apple’s guidance for iOS 26 suggests reducing custom UI overrides (like custom nav bar backgrounds or controls) to let the new system design shine
developer.apple.com
. Our plan follows this: we will trust system defaults for most UI elements and only apply branding colors where it doesn’t break the new aesthetic. For instance, if our app currently has a colored navigation bar, we might switch to using a standard bar with perhaps a translucent colored tint at most, so that it still blends with Liquid Glass effect. By doing so, when iOS updates again or if Apple refines Liquid Glass in 26.x updates, our app will automatically benefit without heavy rework.

In summary, after this UI refresh, the app will look “at home” on iOS 26. Users will notice the app respects their light/dark mode, features the slick glassy visuals (e.g. blurred backgrounds, morphing controls), and overall feels consistent with other updated apps. This addresses point #3 about iOS 26 updates: we’re ensuring support for the Liquid Glass interface with light/dark mode (defaulting to system settings). We will thoroughly test these design changes on real devices (or simulators) running iOS 26 to catch any visual glitches early.

“Fax” Share Sheet Extension Implementation

One of the core features to add is the Share Sheet integration for faxing. This addresses the need for users to quickly send an image or document as a fax from any app. Below is the plan for implementing this Fax action extension:

The iOS 26 share sheet (right) uses a translucent Liquid Glass panel and presents favorite actions as circular icons along the top row, with other actions listed below when expanded
macstories.net
. We will add a “Fax” action icon here, allowing users to send content to our fax service directly from the share menu.

Create the Share Extension Target: We will add a new iOS Share Extension target to our Xcode project. Xcode’s template will generate a stub extension (an .appex bundle) with its own Info.plist and a ShareViewController class
stackoverflow.com
. This extension is essentially a lightweight mini-app that runs when the user invokes the “Fax” action from the share sheet. We’ll name the extension something like “Fax via [AppName]”, but the visible name in the share sheet can simply be “Fax” for clarity (with our app’s icon or a fax glyph). The extension will be configured to run on iPhone (and iPad, if we support iPadOS as well).

Configure Supported Data Types: In the extension’s Info.plist, we need to declare what content types it can handle so that iOS will show our “Fax” option appropriately. We will specify support for image media and possibly PDFs, since those are common fax documents. For example, using the NSExtensionActivationRule, we’ll set keys like NSExtensionActivationSupportsImageWithMaxCount (probably allow up to 10 images at once) and NSExtensionActivationSupportsPDF (if available) to true
stackoverflow.com
. By setting these, iOS will know to list our extension whenever the user shares one or multiple images (or PDF files) from apps like Photos, Camera, or Files. We will start with images since the primary use case described is taking a photo of a document and faxing it – but the architecture will be flexible to add PDFs or other document types later. (If a user tries to share an unrecognized type, our extension simply won’t appear as an option.)

Share Sheet UI/UX Flow: When the user taps the Fax action in the share sheet, the extension’s UI will appear. By default, iOS presents a compose sheet interface for share extensions. We will customize this to gather the fax details:

The extension will show a simple interface prompting the user to select a recipient for the fax. We envision a list or dropdown of saved contacts (names with their fax numbers) for quick selection, along with an option to “Enter a new fax number” manually if needed. For instance, if “Dr. Henderson” is a saved contact with a fax number, the user can just tap that entry. This addresses the scenario given: the user would tap “Fax”, then choose “Dr. Henderson – Fax: 123-456-7890” from the list.

We will implement this selection UI either using the standard SLComposeServiceViewController configuration items or a custom SwiftUI view. One approach is to override configurationItems() in ShareViewController to provide an item like “Recipient: [Dr. Henderson ▾]” which, when tapped, opens a list of contacts. However, since iOS 26 and modern APIs allow SwiftUI, we might instead present a SwiftUI sheet for picking a contact, which could be more flexible in design. We’ll choose the method that provides the best user experience while remaining within extension memory/time constraints.

If the user has no saved contacts yet, the extension can present a field to type in a fax number (and maybe a name label). We might also consider integrating the iOS Contacts picker filtered to contacts that have a fax number, so the user can pick from their address book. This could be a nice addition, but as an initial implementation, a simple manual entry or our app’s own contact list is sufficient.

We will ensure the extension’s UI matches the iOS 26 style as well – likely by using standard controls which will automatically have the new design (rounded corners, etc.). The share sheet itself is now a half-height Liquid Glass panel with inset margins
macstories.net
, so our extension’s interface will appear on that panel. We’ll test the layout so that our custom UI elements (like list of contacts) fit nicely in the new share sheet style (which is larger and more rounded than older iOS versions).

Fax Transmission Trigger (didSelectPost): After the user selects the recipient (and perhaps enters any additional info like a cover page note if we allow), they will tap a Send button (the share sheet’s standard Post button). This invokes the ShareViewController.didSelectPost() method. In this method, we implement the logic to hand off the content (the image(s)) and the chosen fax number to our backend for sending. There are a couple of implementation strategies here:

Directly Upload via API: The extension can take the image data and immediately call our fax-sending API (FaxBot service) to transmit the fax. We would perform an upload to the server with the image file and destination number. To avoid keeping the extension alive too long, we can initiate a background upload task (using a URLSession with background configuration). iOS allows extensions to spawn background URL requests that can continue even after the extension UI is dismissed, as long as we set it up properly. The extension would then quickly terminate while the upload continues on the system’s daemon. Once the fax API call is done, our main app could be notified (e.g., via a push notification or background fetch) to update status.

Hand Off to Main App: Alternatively, the extension can hand the task to the main app. For example, we could save the image to a shared App Group container and then use a custom URL scheme or UIApplication openURL to launch the main app in the background with a command to send the fax. The Stack Overflow example shows saving images to a folder accessible by both the extension and the app
stackoverflow.com
, then invoking the app to handle them. We will use an app group (say, group.com.myapp.faxshare) so both extension and app share files and perhaps UserDefaults. The extension would write the image (or PDF) file into this container and set some flag or data indicating the target fax number. Then it can call self.extensionContext!.completeRequest(...) to close, simultaneously opening the main app via a URL (if allowed). The main app, upon launching or resuming, would detect the pending fax job from the shared data and then use its normal FaxBot integration to send the fax.

We will likely implement a hybrid of these approaches for reliability. The simplest user experience is if the fax sends without needing to foreground the main app, so the background upload approach is appealing. We’ll investigate iOS 26’s capabilities here – Apple continually improves background tasks, so by iOS 26 it might even be possible for extensions to schedule a BGProcessingTask in the container app. If possible, we schedule a background task for the fax send. If not, a background URLSession upload from the extension itself to our server (with a completion handler that triggers a local notification) could work well.

Using FaxBot Service: FaxBot is presumably our backend service or library that actually delivers the fax. We need to integrate it such that from either the extension or the main app, we can invoke a “send fax” operation. Likely, the main app already has code to send faxes (point #6 was yes, indicating this is acceptable). We will expose that functionality to the extension:

If using direct upload, the extension will call the same server API that FaxBot uses (for example, an HTTPS endpoint to send a fax, including authentication). We must ensure the extension has the necessary credentials – perhaps the user’s login token can be shared via the keychain sharing or app group.

If handing off to the app, the app will call FaxBot (maybe a local framework or just construct an API call) using the data saved by the extension.

In either case, FaxBot will handle converting the image(s) into fax format and dialing out the fax. The plan assumes FaxBot’s core logic (such as contacting a third-party fax gateway or a hardware device) is already available for us to use. We just need to feed it the image file and phone number. The integration might be as simple as invoking a REST API: e.g., POST /sendFax with parameters (image file, destination number, sender ID).

We will implement robust error handling: if the fax fails to send (e.g., server returns an error or no phone signal, etc.), the user should be notified. Possibly, the main app can present a notification like “❌ Fax to Dr. Henderson failed. Please check the number and try again.” On success, a notification “✅ Fax sent to Dr. Henderson.” can inform them. Since the extension itself cannot easily keep running to show status, using User Notifications through the main app is a good way to provide feedback. We’ll add code in the app to listen for fax status (perhaps via push from the server or via the background URLSession completion) and fire a local notification accordingly.

UI in Share Sheet (iOS 26 considerations): We will ensure our extension’s presence in the share sheet is user-friendly in the context of iOS 26’s new share sheet design:

In iOS 26, when the share sheet first appears as a half-sheet, the top row shows favorite actions as round icons (monochromatic glyphs) and a “More” button
macstories.net
. We will provide a proper icon for our “Fax” extension – likely a fax machine or document icon – following Apple’s template (single-layer glyph that the system can display). If the user uses fax often, they can pin it as a favorite; otherwise it will appear in the “More” list when the sheet is expanded to full. We should double-check that our extension’s name (“Fax”) and icon appear correctly both in light and dark mode (the system might apply a vibrancy effect).

The share sheet material in iOS 26 is Liquid Glass and becomes more opaque when fully expanded
macstories.net
. Our extension UI will appear on this blurred background. We will design our extension’s interface (text color, table cells for contacts, etc.) to look good on this backdrop. Apple’s default compose sheet style (if we use configurationItems) will likely handle it automatically with a translucent white/dark background on our items. We’ll test the appearance in both modes.

We should be mindful of the share sheet’s new behavior: expanding it requires either dragging up or tapping a “More” action, which could hide our icon behind the expansion if not favored
macstories.net
. This is mostly an Apple UI quirk, but for us it means we should instruct users (perhaps in a tutorial) how to favorite the Fax action for quicker access. (This might be more of a documentation task than development – e.g., a one-time in-app tip that says “Pro Tip: add ‘Fax’ to your Favorites in the iOS share sheet for easy access.”)

Limitations for Initial Release: As noted, we will not handle extremely large faxes in the first version. The extension will accept at most 10 images at once by design
stackoverflow.com
, which should cover typical documents (a multi-page form, etc.). We’ll also set a reasonable file size cap (for example, if an image is somehow 20MB, we might refuse to prevent timeouts). If a user somehow tries to fax an unusually large document, we will show a polite error like “The document is too large to fax. Please try splitting it or contact support.” This is a safeguard; however, as the stakeholder mentioned, “nobody is sending faxes that big”, so it’s low priority. We leave the door open to increasing these limits later (it’s mostly a configuration change and some additional testing in the future).

In summary, the share sheet integration will allow the exact flow the user wants: take a picture -> Share -> Fax -> choose contact -> done. The heavy lifting of sending the fax is done by FaxBot behind the scenes, either via the extension or the main app in background. This covers point #3 (share sheet integration) and point #1 (overall yes to implementing this feature), while respecting the decision in point #2 to skip support for extremely large faxes initially.

Fax Contacts Management & Selection (Recipient Handling)

To make the faxing process smooth, we need a way for users to select or input the fax recipient quickly in the extension. We plan to implement a simple contacts management for fax recipients:

In-App Contact List: We will add a section in the main app where users can save frequent fax recipients (name + fax number, and maybe an optional notes field). This could be as simple as a list of “Saved Fax Contacts” (e.g., doctors, pharmacies, offices, etc.). The user can add a new contact by entering a name and fax number. Having these pre-saved contacts means that in the share extension, we can present a ready list of options instead of forcing the user to type a fax number every time. This addresses point #4 (which was confirmed “yes”) – presumably the idea to have stored fax recipients.

Data Sharing with Extension: The fax contacts data will be stored in a place accessible to both the app and the extension. We will use the same App Group that we use for file sharing. For instance, contacts could be stored in a shared UserDefaults or a small database within the group container. That way, when the share extension runs, it can read the list of contacts instantly. (Note: We must be careful to initialize this data on first use; if the user hasn’t opened the app to create any contacts, the extension should handle that gracefully by showing an “No saved contacts – please enter a fax number” message or even offering to open the main app to create one.)

UI in Extension for Contact Selection: As described earlier, the extension will show a list of contacts if available. We will sort them alphabetically, or possibly by most recent use (if we track whom the user faxed most often, we could show those on top). If there are many contacts, we might include a search bar to filter by name. Given the likely scope, users might have just a handful of fax contacts (unlike a phone contacts list), so a simple list might suffice.

Manual Entry Fallback: We will include an “Other…” or “New Fax Number” option in the share sheet extension. This brings up a field to type a fax number (and maybe a name, in case we want to offer saving it). This is important for one-off faxes. We will need to validate the input (ensure it’s numeric or in proper format) before allowing the send.

Integration with iOS Contacts (Future): As an enhancement (possibly beyond this project’s initial scope), we could integrate the native Contacts picker. iOS Contacts have a phone label for fax numbers. In the future, we might allow the user to tap “Import from Contacts” and choose a contact’s fax number to add to saved contacts. This is a “nice-to-have” and not required for the core functionality, but it aligns with providing a seamless experience. We will design the system in a way that adding this later is straightforward (our data model can accommodate multiple numbers, etc., if needed).

By implementing contacts management, we satisfy the use case of selecting “Dr. Henderson – Fax number” in the share flow without hassle. This feature will be tested alongside the share extension to ensure that saved contacts show up correctly and that we can retrieve the fax number for sending. It also aligns with point #4 (which the user agreed to), confirming that having a selection of recipients is desired.

Background Fax Sending & Notifications

After the user initiates the fax via the share sheet, the actual sending process happens without further user interaction. This part of the plan ensures that the FaxBot service works behind the scenes and informs the user of the result:

Using Background Tasks: We will utilize iOS background capabilities to send the fax so that the user does not need to keep any UI open. As noted, if we use a background URLSession in the extension, the system can continue the upload even after the extension UI is gone. Alternatively, if we hand off to the main app, we can use BGTaskScheduler to run a background task (if iOS permits on-demand scheduling) to complete the fax send. We will research which method is more reliable under iOS 26. The priority is that the fax gets sent even if the user immediately goes back to what they were doing.

Performance Considerations: Faxing an image involves uploading potentially a few megabytes of data (if it’s a photo of a document). We expect this to complete in a matter of seconds on a good connection. However, if the network is slow or the file is large, it could take longer. We will set appropriate timeout intervals and maybe compress/resize the image if it’s extraordinarily high resolution (since fax resolution doesn’t need a 12MP photo). This will help ensure the upload completes in the limited background time window. iOS typically gives extensions only a short time (e.g. ~30 seconds) to finish tasks after the UI dismisses, unless using the background transfer API which offloads it to the system. We will leverage those APIs to avoid being cut off mid-send.

Status Tracking: We need a mechanism for knowing whether the fax was sent successfully or if an error occurred:

If we do a direct API call from the extension, we will get a response (success/fail) from our server. We might not be able to show a UI at that point, but we can communicate it to the main app. For example, upon completion, the extension (or the background URLSession completion handler in the app) can store the result in the shared container or schedule a local notification.

If we invoke the main app to send, the main app can directly show a progress UI or immediately background itself. However, auto-launching the app may not always be user-friendly (imagine they were in Photos, suddenly our app pops up). We have to be careful: maybe we only trigger the app launch if absolutely necessary (like multiple images that need processing beyond extension capabilities).

We will likely lean on the background upload approach to keep everything invisible to the user until they get a notification.

User Notification of Outcome: To close the loop, we will implement user notifications for fax results:

On a successful fax send, the app will schedule a local notification saying something like “✅ Your fax to Dr. Henderson was sent successfully.” This gives the user peace of mind. We could include an identifier (like the recipient name or number) in the notification.

On a failure, e.g., if the fax service returns an error (number busy, or no answer, or any network issue), we’ll send a notification: “❌ Fax to Dr. Henderson failed to send. Tap to retry.” We can direct the tap action to open the app’s fax history or retry interface. Retrying might simply re-trigger the fax send in the app, possibly after the user fixes any issue (or it might succeed on second try if it was a transient error).

We will register the app for the necessary notification permissions if not already done. Since these are local notifications, the user just needs to have allowed our app to send notifications.

Fax History and In-App Status: In addition to notifications, we will have an Outbox/History in the main app showing recent faxes and their status (sent, failed, pending). This way, if a user opens the app later, they can confirm that “Yes, the fax I sent earlier went through” or see if it’s still sending. This history can draw from the same shared data; for example, we log an entry when a fax is initiated, and update it when completed. This feature aligns with providing transparency and will be part of the testing (point #5 likely covered ensuring everything works, which was confirmed “yes”).

Security & Privacy: We should mention that when we handle potentially sensitive documents (like an insurance card photo), we treat them securely. The shared container and any uploaded content should be secured. We will ensure the App Group container is properly configured, and clear out any temporary files after use. If using a server, we’ll use HTTPS and not store the fax image longer than needed on the device or server. These details reinforce trust in using the fax feature for personal documents.

By handling background sending and notifications in this way, the fax process becomes fire-and-forget for the user: they initiate it and can go about their day, and they’ll be informed when it’s done. This fulfils the requirement that it “run through faxbot in the background” (point #3 detail), and covers our promise of a seamless experience.

Testing Plan

To ensure reliability and a smooth launch, we will conduct thorough testing of both the new iOS 26 UI updates and the share sheet fax feature:

Unit Testing & Module Testing: We will write unit tests for any new components (if applicable, e.g., a function that converts images to fax format, or the contact list persistence). For the extension, we might simulate the behavior of selecting an item to ensure the data passed to the fax-sending logic is correct.

Integration Testing of Share Extension: Since share extensions are a bit tricky to test, we will do a lot of manual and device testing:

On a device (or simulator) running iOS 26, install the app and ensure the “Fax” option appears in the share sheet when it should. We’ll test with the Photos app: take a picture, open it, tap share, verify our extension icon is listed (possibly under “More” if not auto-favorited). We’ll also test sharing multiple images at once (select several in Photos, then share) to see if our extension can handle multiple attachments properly.

Test from the Camera app directly: After taking a photo, the user can tap the share icon from the preview. We need to ensure our extension shows up there as well (it should, as it’s the same mechanism). This fulfills the exact use-case described (photo of insurance card -> share -> fax).

Test sharing a PDF from the Files app (if we decide to support PDFs initially). If not initially, skip for now.

Verify the extension’s UI: select different contacts, use the manual entry, and press send. We will simulate both success and failure scenarios (we might point the extension to a test/staging FaxBot endpoint that intentionally returns a success or error).

Ensure that after pressing send, the extension dismisses and control returns to the host app smoothly.

Background Sending & Notification Testing: We will simulate various conditions:

Successful send: Use a small image and live network to see that our server call completes. Check that a notification is received with success message. Also check that the fax history in-app marks it as sent.

Failure cases: Simulate a network failure (e.g., turn on Airplane mode or point to an invalid server temporarily) and try sending. The extension should handle it gracefully (perhaps by showing an error immediately or just failing silently but logging). Ensure a failure notification appears and is worded appropriately.

Edge cases: Very large image (if someone tries a panorama photo, for instance) – see if our size limit logic works (maybe the extension refuses due to size and alerts the user, or automatically downsizes). Multiple images – see if they all get sent (our FaxBot may need to combine them, we verify the fax received has all pages).

No contact selected: Ensure we don’t let the user send without choosing or entering a fax number – the Send button should be disabled until a recipient is provided (this will be part of isContentValid() in the extension, returning false if no recipient chosen, so the share sheet’s post button is disabled by default
stackoverflow.com
).

Performance: Measure how long the extension takes to process and exit. It should ideally finish quickly (a few seconds). We should avoid any long-running tasks in the extension’s main thread that could make the share sheet seem unresponsive.

UI/UX Review in Light vs Dark: We will run the app and extension in both light and dark mode on iOS 26 to ensure the colors and Liquid Glass effects look good. For example, in dark mode the share sheet Liquid Glass has a dark blur – we’ll verify our text (which is likely light colored in dark mode) is readable. We’ll also test on different background content (if user’s photo is very bright vs very dark, does our UI still show up clearly?).

Device Compatibility: Although the focus is iOS 26, we should test on slightly older versions (iOS 25 or 24 if we still support them) to ensure nothing breaks. The new UI features might degrade gracefully on older iOS. Our extension should ideally be backward-compatible to at least iOS 25 (if needed), albeit without Liquid Glass visuals. We’ll confirm that the share extension still shows up and works on older iOS for users who haven’t upgraded, if we choose to support that.

TestFlight Beta: Once we are satisfied internally, we will do a TestFlight beta release for the team or a small group of users. They can try out faxing via share sheet in real-world scenarios and give feedback. Particular attention will be on:

Did the fax send reliably each time?

Was the share sheet experience intuitive?

Any suggestions on UI wording (maybe label the extension “Fax via MyApp” if just “Fax” was confusing? We’ll gauge understanding).

Any issues with the new design on iOS 26 (e.g., “screen X looks odd in dark mode”).

Issues found during testing will be tracked and fixed prior to the public launch. Our goal is to catch edge cases so that by release, the feature is robust.

Timeline & Milestones

Below is a proposed timeline for implementing these features (assuming development can start immediately):

Research & Design (1 week): Completed initial research on iOS 26 design changes and share extension (✅ done as part of this planning). Next, finalize the UI design updates (colors, removed custom elements) and the extension UI flow. Create wireframes for the share sheet extension screen (contact picker interface, etc.). Define the data model for saved fax contacts.

Development Phase 1 – iOS 26 UI Updates (1–2 weeks): Implement the visual/style changes in the main app:

Update navigation bar, tab bar appearances to use new defaults.

Update colors to dynamic system colors.

Test and tweak various screens for the new look (rounded tables, etc.).

Implement any needed icon asset adjustments (for app icon and any SF Symbol usage for new weights).

Development Phase 2 – Share Extension Feature (2–3 weeks): Start building the share extension:

Set up the extension target and get it to appear in the share sheet with a dummy action.

Implement the extension UI for selecting a fax recipient. At first, hard-code a couple of test contacts or use manual entry until the contact list from app is ready.

Code the logic in didSelectPost() to gather the image(s) and recipient info, and then hand off to FaxBot. Integrate with a test endpoint or stub to simulate fax sending.

Also in parallel, add the App Group entitlement and set up shared container for data (files and contacts).

Ensure basic end-to-end flow: e.g., run on device, share an image, pick a number, and see that the app (or server stub) receives the data.

Development Phase 3 – FaxBot Integration & Contacts (1–2 weeks):

Hook up the actual FaxBot service/API. Insert real API keys or endpoints and test sending a fax for real (perhaps to a test fax number or a monitoring service). Work out any issues in authentication or file formatting.

Implement the Saved Contacts list in the main app and the ability for the extension to read it. This likely involves writing a small Contacts manager class and using UserDefaults or a file in the shared container. Also implement the UI in-app to add/edit contacts.

Refine the extension UI to use real contacts from that list. Ensure that if the list is empty, the extension handles it (maybe directly goes to an input mode).

Add the notification scheduling in the main app for fax results. This may involve implementing URLSession delegate in the app for background transfers, or handling openURL callbacks from the extension, etc., depending on approach. Test that a notification is triggered appropriately.

Testing & Bug Fixing (1–2 weeks): As outlined in the testing section, thoroughly test all scenarios. Fix any issues:

UI glitches (e.g., extension sheet layout issues).

Functional bugs (e.g., fax not actually sending, or multiple images out of order).

Performance problems (e.g., extension taking too long causing a timeout).

Incorporate feedback from any team testers.

Polish & App Store Prep (0.5 week): Update app metadata if needed (we might mention in release notes “Now with iOS 26 support and Share Sheet Faxing!”). Prepare screenshots if we want to highlight the new feature (though share sheet might be hard to capture, perhaps we can show an in-app screenshot of the new UI or mention it textually). Ensure the extension’s icon and display name are correct. Double-check that the app group and extension are properly included in the release build (sometimes forgetting to include the .appex can happen, we’ll verify it’s in the .ipa
stackoverflow.com
).

Release: Target a release date that doesn’t conflict with any major events. Since iOS 26 is out, sooner is better to delight users. Once released, monitor analytics or logs for fax usage and any errors (we could add logging for fax failures to our server to catch if something goes wrong in the wild). Also be ready to issue a quick patch update if any critical bug appears post-release.

Throughout the project, we’ll keep communication open with the team/stakeholders, especially if we discover any constraints or needed changes (for example, if Apple has any App Store guideline about faxing or extensions we need to heed). Given the user’s urgency (“YES I WANT THE ENTIRE FULL PLAN”), we have outlined everything comprehensively to avoid back-and-forth.

Conclusion

By following this plan, we will deliver a fully updated iOS 26 experience in the app, along with the highly requested Share Sheet Fax functionality. The app will sport a fresh look using Apple’s Liquid Glass design (with adaptive light/dark mode), ensuring it doesn’t feel outdated on the latest iPhones. More importantly, users will be able to send faxes in a couple of taps from anywhere on their device – a significant convenience upgrade.

We accounted for the key points:

We agreed to implement the full plan (#1 YES),

We decided not to worry about extremely large faxes initially (#2 defer as they are rare, can add later),

We incorporated iOS 26-specific design considerations and the share sheet extension (#3 YES, with research-backed approach to Liquid Glass and share sheet integration),

We will include the necessary features like contact selection (#4 YES),

We will test thoroughly and update all relevant aspects (#5 YES),

We will integrate everything with the existing FaxBot service (#6 YES).

With this plan in place, the development team can proceed confidently, and we anticipate meeting the user’s needs effectively. The result will be a modernized app that not only meets the iOS 26 design standards but also dramatically improves the faxing workflow for end users, turning a formerly tedious task into a quick share action.

all citations retained as per research:
apple.com
apple.com
macrumors.com
macstories.net
stackoverflow.com
stackoverflow.com