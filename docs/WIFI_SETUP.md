# Offline Wi-Fi setup and recovery

The Raspberry Pi can create an open **CTS-Scoreboard** Wi-Fi network to let a
phone, tablet, or laptop configure its upstream Wi-Fi. The setup page uses the
same web-admin account as Settings. Azure and an internet connection are not
required for the setup page or the local scoreboard.

## Prepare the device

Install the application and Pi provisioning integration before taking the Pi
offline. A blank Raspberry Pi OS image does not contain this application or
its operating-system dependencies. See [Pi installation](../README.md#set-up-the-raspberry-pi)
and the [kiosk guide](PI_KIOSK_SETUP.md).

The implementation has local automated coverage, but Pi radio behavior and
phone interoperability still require hardware acceptance. Before relying on
this flow at a meet, exercise cold boot, failed credentials, DHCP failure,
refresh/rejoin, successful handoff, and service restart on a designated Pi
with wired or local recovery access. Confirm that no setup DHCP/DNS traffic
survives station activation. Captive-popup behavior must be checked on the
actual phones/tablets used by operators.

Provisioning is enabled by default in the kiosk installer, which starts the
controller immediately. Run installation/upgrades with local or wired recovery
access: the controller may change the Wi-Fi connection.

```bash
cd ~/scoreboard
./pi/scripts/install-kiosk.sh
```

Use `./pi/scripts/install-kiosk.sh --no-wifi` if you want the kiosk without
the Wi-Fi controller. The other installer escape hatches remain available
(`--dry-run`, `--no-apt`, `--no-blanking`, `--no-linger`).

The initial target is Raspberry Pi 5, Raspberry Pi OS Bookworm, and
NetworkManager. The desktop/labwc session is needed for HDMI kiosk and hotkeys,
not for the network controller. The controller is a separate system service;
the Flask app and Chromium remain unprivileged. It must not be launched using
a root shell in the user's checkout.

The managed radio must support AP mode and have a valid regulatory
configuration. If Wi-Fi is rfkill-blocked, unsupported, or managed by another
network stack, fix the underlying OS configuration using wired or local access
rather than repeatedly retrying the web form.

## Join and configure

1. On first setup, or when saved Wi-Fi connections cannot be established and
   Ethernet is not usable, the Pi displays Wi-Fi setup instructions on HDMI.
   For manual setup, press **Ctrl+Alt+W** on the Pi's keyboard.
2. Join **CTS-Scoreboard** on the management device. No Wi-Fi password is
   required. A displayed Wi-Fi QR code can help join supported devices.
3. If a sign-in window does not open, use the explicit **HTTP** setup URL
   shown on HDMI in a normal browser. Remain connected to the network even if
   the phone warns that it has no internet access.
4. Sign in with the Pi's existing admin account. On an unchanged new
   installation the defaults are `admin` / `password`; use your configured
   account instead when those have been changed.
5. Select the network that the management device will also use. Prefer a
   network with internet access if you need cloud publishing. Saved, personal
   password-protected, open, and hidden networks have distinct connection
   paths; enterprise/802.1X provisioning is not provided.
6. Read the reconnect instructions before confirming the handoff. The Pi
   must stop its hotspot, DHCP, and DNS before it can join the new network.
   The phone losing contact with the hotspot is expected, not proof of success.
7. Join the selected network on the phone, then open the Pi's local URL.
   HDMI returns to the scoreboard and shows the assigned LAN address after
   a successful connection. Use the numeric address if `.local` discovery
   is unavailable.

The setup address is displayed by the Pi. It may differ when a private subnet
conflicts with existing local routes; use the displayed address instead of
assuming a fixed IP. The installer enables Avahi for `.local` discovery;
networks that filter multicast still require the numeric address. Each Pi
retains its own hostname. If several Pi devices
advertise the same setup SSID, confirm the device identity shown by the page
and HDMI.

## Recovery and normal operation

- Short network interruptions do not immediately force setup. Sustained loss
  of Wi-Fi with no usable Ethernet triggers recovery; internet failure alone
  does not. Manual Ctrl+Alt+W can enter setup even while Ethernet is connected.
- A failed join restores **CTS-Scoreboard** and retains an actionable error.
  Rejoin it, sign in if needed, and correct the selected network/password.
  Previously working profiles are preserved.
- Cancel an uncommitted attempt without changing networking. **Retry saved
  networks** makes an explicit reconnect attempt and restores setup if none
  can be used. A first-boot device should never be left with no recovery path.
- A radio may be unable to scan fresh networks while acting as an AP.
  The page shows cached results and offers manual/hidden SSID entry. An
  explicit refresh may warn that it needs to disconnect the hotspot briefly;
  reconnect afterward for the updated list. Background polling must not cause
  such interruptions.
- A local connection with a usable IP is enough to return to normal mode.
  Internet availability, captive access, and unknown connectivity are
  separate advisory states.
- Existing kiosk keys remain available: Ctrl+Alt+K exits, Ctrl+Alt+S opens
  Settings, and Ctrl+Alt+R reopens the kiosk. The automatic setup display is
  local to the kiosk; normal remote scoreboard pages remain scoreboard pages.

## Security and network isolation

**The open hotspot and HTTP setup connection are unencrypted.** App
authentication restricts configuration access but does not protect credentials
from nearby eavesdroppers or authenticate the Wi-Fi hotspot to the phone.
This includes both the web-admin password and the submitted upstream Wi-Fi
password. Use setup only in a trusted environment; Ethernet or local access
is preferable when that exposure is unacceptable. Passwordless Wi-Fi
encryption (OWE) is not implemented.

The controller uses a dedicated NetworkManager AP profile and an AP-scoped
dnsmasq process. It does not provide internet connection sharing or modify
NetworkManager's global DNS mode. AP-only DHCP/DNS and HTTP interception are
removed **before** upstream activation. If teardown fails, joining must stop
instead of risking interference with venue network infrastructure.

The installer and service manager only supervise `cts-wifi-provisioning.service`.
Any AP/DHCP/DNS/firewall lifecycle is owned internally by that controller
process; no separate `cts-wifi-provisioning-dnsmasq`, `cts-wifi-provisioning-nft`,
or `cts-wifi-provisioning-intercept` unit is expected.

The setup profile has a stable controller-owned UUID; an unrelated saved
profile with the same display name is not modified. New join credentials are
staged in a non-autoconnecting NetworkManager profile and promoted only after
the intended Wi-Fi profile has a usable local address. Interrupted staging
profiles are identified by a controller tag and cleaned up on restart.

Network-changing requests require the admin session and a CSRF token.
The local keyboard trigger uses a restricted Unix socket, not an unauthenticated
HTTP endpoint. Kiosk status is read-only and restricted to loopback clients.
Avoid exposing port 5000 directly to the internet.

## Mobile and venue limitations

Captive sign-in popups are a convenience, not a guarantee. iOS/iPadOS,
Android, and laptops may decline to open one, prefer cellular data, or close
the popup when the hotspot disappears. The fallback is the displayed HTTP
URL in a regular browser. A website cannot reliably switch a phone's Wi-Fi
network on the user's behalf.

Arbitrary HTTPS/HSTS websites cannot be redirected without certificate
errors. This implementation does not intercept TLS. DHCP option 114 refers
to a standards-based captive-portal **API**, which requires trusted HTTPS;
it must not point to this HTTP setup page.

Apple Wireless Accessory Configuration is an MFi accessory mechanism, not a
general browser credential-sharing API. Android Wi-Fi Easy Connect/DPP can
transfer credentials on supported hardware, but needs a compatible device
enrollee/driver/supplicant. Neither is required or promised here. The displayed
open-network QR code is not DPP.

A venue can allow Wi-Fi association but still require a browser login for
internet access. Signing into that portal on the phone does not generally
authorize the Pi, because they have different network identities. Core setup
reports the connectivity limitation; it does not automate venue login.
Phone-only venue-login support is a separately gated follow-up.

Guest/client isolation can also stop the phone from reaching the Pi on the
same SSID. Use a LAN that permits local device-to-device communication, or
ask venue IT for a suitable network/device registration. Do not mistake
isolation for a wrong password.

## Diagnostics and maintenance

Keep a wired or local recovery path available before intentionally changing
the Pi's network remotely. For controller failures, inspect its system service:

```bash
systemctl status cts-wifi-provisioning.service
journalctl -u cts-wifi-provisioning.service

# If the user service should carry the provisioning env/drop-in:
systemctl --user status cts-scoreboard.service
systemctl --user cat cts-scoreboard.service
```

The web app runs separately:

```bash
systemctl --user status cts-scoreboard.service
journalctl --user -u cts-scoreboard.service
```

Do not run multiple network controllers against the same radio or start a
global dnsmasq/hostapd instance alongside the managed setup flow. An update
must refresh the root-owned controller installation as well as the user
checkout. Uninstall through the supplied scripts so only owned services,
AP profiles, and firewall rules are removed; retain upstream saved profiles.
Allow the controller to finish cleanup rather than forcing it to stop during a
handoff. The installer/uninstaller checks the service's shutdown result and
stops if cleanup failed; inspect the controller journal before retrying.

## Standards references

- [Apple captive-network guidance](https://developer.apple.com/news/?id=q78sq5rv)
- [Android captive-portal detection](https://developer.android.com/about/versions/11/features/captive-portal)
- [RFC 8910: Captive-Portal Identification](https://www.rfc-editor.org/info/rfc8910)
- [RFC 8908: Captive Portal API](https://www.rfc-editor.org/info/rfc8908)
- [Android Wi-Fi Easy Connect](https://developer.android.com/develop/connectivity/wifi/wifi-easy)
