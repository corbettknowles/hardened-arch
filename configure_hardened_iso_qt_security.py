#!/usr/bin/env python3
"""
Finalize Hardened Arch ISO integration after the X.Org source build.

This script:
  * builds a Qt 6 live installer front-end and HTTPS update fetcher;
  * installs dedicated Limine boot targets for install, update, verify,
    and password-protected recovery;
  * disables Limine command-line editing;
  * removes the early-initramfs diagnostic file logger and every
    unrestricted early shell path;
  * locks passwordless root in the image and removes root autologin;
  * transfers the password created by the Qt installer into the installed OS;
  * changes ISO/update checksums from SHA-256 to SHA-512;
  * verifies the generated ISO against its .sha512 sidecar immediately.

Run directly (no wrapper/pipeline):

    sudo python3 /home/corbett/configure_hardened_iso_qt_security.py
"""

from __future__ import annotations

import argparse
import ast
import os
import py_compile
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_BUILDER = Path("/home/corbett/build_hardened_arch_iso.py")
DEFAULT_RUNTIME_ROOT = Path("/home/corbett/xorg-source-stage/rootfs")
DEFAULT_TOOLS_ROOT = Path("/home/corbett/hardened-qt-tools")
DEFAULT_QT_PREFIXES = (
    Path("/home/corbett/kde/usr"),
    Path("/usr"),
)


class PatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Paths:
    builder: Path
    runtime_root: Path
    tools_root: Path
    source_dir: Path
    build_dir: Path
    install_dir: Path
    qt_binary: Path


def run(
    cmd: Sequence[str | Path],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> str:
    argv = [str(item) for item in cmd]
    print("+", " ".join(argv))
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    if proc.returncode != 0:
        if capture and proc.stdout:
            print(proc.stdout, end="")
        raise PatchError(f"exit {proc.returncode}: {' '.join(argv)}")
    return proc.stdout.strip() if capture and proc.stdout else ""


def require_root() -> None:
    if os.geteuid() != 0:
        raise PatchError("run this script with sudo")


def write_text(path: Path, data: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")
    path.chmod(mode)


def backup_once(path: Path, suffix: str) -> Path:
    backup = path.with_name(path.name + suffix)
    if not backup.exists():
        shutil.copy2(path, backup)
        print(f"BACKUP: {backup}")
    else:
        print(f"BACKUP EXISTS: {backup}")
    return backup


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add Qt install/update modes, authenticated recovery, and SHA-512 ISO verification."
    )
    parser.add_argument("--builder", type=Path, default=DEFAULT_BUILDER)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--tools-root", type=Path, default=DEFAULT_TOOLS_ROOT)
    parser.add_argument("--qt-prefix", type=Path)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument(
        "--manifest-url",
        default="",
        help="Optional HTTPS latest.json URL. The ISO builder's --repo-url still overrides this per build.",
    )
    parser.add_argument("--skip-qt-build", action="store_true")
    return parser.parse_args()


def make_paths(args: argparse.Namespace) -> Paths:
    tools_root = args.tools_root.expanduser().resolve()
    install_dir = tools_root / "install"
    return Paths(
        builder=args.builder.expanduser().resolve(),
        runtime_root=args.runtime_root.expanduser().resolve(),
        tools_root=tools_root,
        source_dir=tools_root / "src",
        build_dir=tools_root / "build",
        install_dir=install_dir,
        qt_binary=install_dir / "usr/local/bin/hardened-live-qt",
    )


CPP_SOURCE = r'''#include <QApplication>
#include <QBoxLayout>
#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QFont>
#include <QFormLayout>
#include <QGroupBox>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>
#include <QLabel>
#include <QLineEdit>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QPlainTextEdit>
#include <QProcess>
#include <QProgressBar>
#include <QPushButton>
#include <QSaveFile>
#include <QStandardPaths>
#include <QTextCursor>
#include <QStringList>
#include <QUrl>
#include <QWidget>

#include <sys/stat.h>
#include <utility>

class HardenedWindow final : public QWidget {
public:
    explicit HardenedWindow(QString mode, QWidget *parent = nullptr)
        : QWidget(parent), mode_(std::move(mode)) {
        setWindowTitle(mode_ == "installer"
                           ? "Hardened Arch Linux Installer"
                           : "Hardened Arch Linux Update Fetcher");
        resize(900, 680);

        auto *layout = new QVBoxLayout(this);
        auto *title = new QLabel(windowTitle(), this);
        QFont titleFont = title->font();
        titleFont.setPointSize(titleFont.pointSize() + 6);
        titleFont.setBold(true);
        title->setFont(titleFont);
        layout->addWidget(title);

        status_ = new QLabel("Root authentication has not been initialized for this live session.", this);
        status_->setWordWrap(true);
        layout->addWidget(status_);

        passwordBox_ = new QGroupBox("Create the root password", this);
        auto *passwordLayout = new QFormLayout(passwordBox_);
        password1_ = new QLineEdit(passwordBox_);
        password2_ = new QLineEdit(passwordBox_);
        password1_->setEchoMode(QLineEdit::Password);
        password2_->setEchoMode(QLineEdit::Password);
        password1_->setPlaceholderText("At least 12 characters");
        password2_->setPlaceholderText("Enter it again");
        passwordButton_ = new QPushButton("Set and unlock root password", passwordBox_);
        passwordLayout->addRow("Password:", password1_);
        passwordLayout->addRow("Confirm:", password2_);
        passwordLayout->addRow(passwordButton_);
        layout->addWidget(passwordBox_);

        if (mode_ == "update") {
            auto *updateBox = new QGroupBox("Read-only HTTPS update source", this);
            auto *updateLayout = new QFormLayout(updateBox);
            manifestUrl_ = new QLineEdit(readManifestUrl(), updateBox);
            checkButton_ = new QPushButton("Fetch latest.json", updateBox);
            downloadButton_ = new QPushButton("Download and verify SHA-512", updateBox);
            downloadButton_->setEnabled(false);
            progress_ = new QProgressBar(updateBox);
            progress_->setRange(0, 100);
            updateLayout->addRow("Manifest URL:", manifestUrl_);
            updateLayout->addRow(checkButton_);
            updateLayout->addRow(downloadButton_);
            updateLayout->addRow(progress_);
            layout->addWidget(updateBox);
        } else {
            auto *installerButtons = new QHBoxLayout();
            startButton_ = new QPushButton("Start installer", this);
            sendInput_ = new QLineEdit(this);
            sendInput_->setPlaceholderText("Reply to an installer prompt, then press Enter");
            sendButton_ = new QPushButton("Send", this);
            cancelButton_ = new QPushButton("Cancel installer", this);
            installerButtons->addWidget(startButton_);
            installerButtons->addWidget(sendInput_, 1);
            installerButtons->addWidget(sendButton_);
            installerButtons->addWidget(cancelButton_);
            layout->addLayout(installerButtons);
            sendInput_->setEnabled(false);
            sendButton_->setEnabled(false);
            cancelButton_->setEnabled(false);
        }

        output_ = new QPlainTextEdit(this);
        output_->setReadOnly(true);
        output_->setLineWrapMode(QPlainTextEdit::NoWrap);
        layout->addWidget(output_, 1);

        auto *bottom = new QHBoxLayout();
        bottom->addStretch(1);
        rebootButton_ = new QPushButton("Reboot", this);
        poweroffButton_ = new QPushButton("Power off", this);
        bottom->addWidget(rebootButton_);
        bottom->addWidget(poweroffButton_);
        layout->addLayout(bottom);

        connect(passwordButton_, &QPushButton::clicked, this, [this] { setRootPassword(); });
        connect(rebootButton_, &QPushButton::clicked, this, [] {
            QProcess::startDetached("/usr/bin/systemctl", {"reboot"});
        });
        connect(poweroffButton_, &QPushButton::clicked, this, [] {
            QProcess::startDetached("/usr/bin/systemctl", {"poweroff"});
        });

        if (mode_ == "update") {
            connect(checkButton_, &QPushButton::clicked, this, [this] { fetchManifest(); });
            connect(downloadButton_, &QPushButton::clicked, this, [this] { downloadIso(); });
        } else {
            connect(startButton_, &QPushButton::clicked, this, [this] { startInstaller(); });
            connect(sendButton_, &QPushButton::clicked, this, [this] { sendInstallerInput(); });
            connect(sendInput_, &QLineEdit::returnPressed, this, [this] { sendInstallerInput(); });
            connect(cancelButton_, &QPushButton::clicked, this, [this] {
                if (installer_.state() != QProcess::NotRunning)
                    installer_.terminate();
            });
            installer_.setProcessChannelMode(QProcess::MergedChannels);
            connect(&installer_, &QProcess::readyReadStandardOutput, this, [this] {
                append(QString::fromLocal8Bit(installer_.readAllStandardOutput()));
            });
            connect(&installer_, qOverload<int, QProcess::ExitStatus>(&QProcess::finished),
                    this, [this](int code, QProcess::ExitStatus status) {
                        append(QString("\nInstaller ended: code %1, status %2\n")
                                   .arg(code)
                                   .arg(status == QProcess::NormalExit ? "normal" : "crashed"));
                        sendInput_->setEnabled(false);
                        sendButton_->setEnabled(false);
                        cancelButton_->setEnabled(false);
                        startButton_->setEnabled(rootReady_);
                    });
        }

        rootReady_ = rootHasPassword();
        updateRootUi();
    }

private:
    QString mode_;
    bool rootReady_ = false;
    QLabel *status_ = nullptr;
    QGroupBox *passwordBox_ = nullptr;
    QLineEdit *password1_ = nullptr;
    QLineEdit *password2_ = nullptr;
    QPushButton *passwordButton_ = nullptr;
    QPlainTextEdit *output_ = nullptr;
    QPushButton *rebootButton_ = nullptr;
    QPushButton *poweroffButton_ = nullptr;

    QProcess installer_;
    QPushButton *startButton_ = nullptr;
    QLineEdit *sendInput_ = nullptr;
    QPushButton *sendButton_ = nullptr;
    QPushButton *cancelButton_ = nullptr;

    QNetworkAccessManager network_;
    QLineEdit *manifestUrl_ = nullptr;
    QPushButton *checkButton_ = nullptr;
    QPushButton *downloadButton_ = nullptr;
    QProgressBar *progress_ = nullptr;
    QJsonObject isoObject_;
    QString releaseVersion_;
    QNetworkReply *downloadReply_ = nullptr;
    QFile downloadFile_;
    QString finalDownloadPath_;

    void append(const QString &text) {
        output_->moveCursor(QTextCursor::End);
        output_->insertPlainText(text);
        output_->moveCursor(QTextCursor::End);
    }

    static QString rootHash() {
        QFile shadow("/etc/shadow");
        if (!shadow.open(QIODevice::ReadOnly | QIODevice::Text))
            return {};
        while (!shadow.atEnd()) {
            const QByteArray line = shadow.readLine().trimmed();
            if (!line.startsWith("root:"))
                continue;
            const QList<QByteArray> fields = line.split(':');
            return fields.size() > 1 ? QString::fromLatin1(fields.at(1)) : QString{};
        }
        return {};
    }

    static bool rootHasPassword() {
        const QString hash = rootHash();
        return !hash.isEmpty() && hash != "!" && hash != "*" && !hash.startsWith("!");
    }

    void updateRootUi() {
        passwordBox_->setVisible(!rootReady_);
        if (rootReady_) {
            status_->setText("Root is password-protected. No passwordless root shell is available.");
        }
        if (startButton_)
            startButton_->setEnabled(rootReady_ && installer_.state() == QProcess::NotRunning);
        if (checkButton_)
            checkButton_->setEnabled(rootReady_);
    }

    void setRootPassword() {
        const QString first = password1_->text();
        const QString second = password2_->text();
        if (first.size() < 12) {
            status_->setText("The root password must contain at least 12 characters.");
            return;
        }
        if (first != second) {
            status_->setText("The two root passwords do not match.");
            return;
        }

        QProcess chpasswd;
        chpasswd.setProgram("/usr/bin/chpasswd");
        chpasswd.start();
        if (!chpasswd.waitForStarted(5000)) {
            status_->setText("Unable to start chpasswd.");
            return;
        }
        chpasswd.write("root:" + first.toUtf8() + "\n");
        chpasswd.closeWriteChannel();
        if (!chpasswd.waitForFinished(30000) || chpasswd.exitCode() != 0) {
            status_->setText("chpasswd rejected the password.");
            append(QString::fromLocal8Bit(chpasswd.readAllStandardError()));
            return;
        }

        password1_->clear();
        password2_->clear();
        const QString hash = rootHash();
        if (hash.isEmpty() || hash == "!" || hash.startsWith("!")) {
            status_->setText("The root account remained locked after chpasswd.");
            return;
        }

        QDir().mkpath("/run/hardened-live");
        QFile hashFile("/run/hardened-live/root-password.hash");
        if (!hashFile.open(QIODevice::WriteOnly | QIODevice::Truncate | QIODevice::Text)) {
            status_->setText("Password was set, but its installer-transfer record could not be created.");
            return;
        }
        hashFile.write(hash.toUtf8());
        hashFile.write("\n");
        hashFile.close();
        ::chmod("/run/hardened-live/root-password.hash", S_IRUSR | S_IWUSR);

        rootReady_ = true;
        updateRootUi();
        append("Root password created and root account unlocked for authenticated login.\n");
    }

    void startInstaller() {
        if (!rootReady_) {
            status_->setText("Create the root password before starting installation.");
            return;
        }
        if (!QFileInfo::exists("/usr/local/sbin/hardened-install")) {
            status_->setText("Missing installer backend: /usr/local/sbin/hardened-install");
            return;
        }
        output_->clear();
        installer_.setProgram("/usr/local/sbin/hardened-install");
        installer_.setArguments({});
        installer_.start();
        if (!installer_.waitForStarted(5000)) {
            status_->setText("The installer backend failed to start.");
            return;
        }
        startButton_->setEnabled(false);
        sendInput_->setEnabled(true);
        sendButton_->setEnabled(true);
        cancelButton_->setEnabled(true);
        status_->setText("Installer running. Type replies in the input field when it prompts.");
    }

    void sendInstallerInput() {
        if (installer_.state() == QProcess::NotRunning)
            return;
        const QByteArray reply = sendInput_->text().toUtf8() + '\n';
        installer_.write(reply);
        sendInput_->clear();
    }

    static QString readManifestUrl() {
        QFile config("/etc/hardened-arch/update.conf");
        if (!config.open(QIODevice::ReadOnly | QIODevice::Text))
            return {};
        while (!config.atEnd()) {
            const QString line = QString::fromUtf8(config.readLine()).trimmed();
            if (line.startsWith("MANIFEST_URL="))
                return line.mid(QString("MANIFEST_URL=").size()).trimmed();
        }
        return {};
    }

    bool validHttps(const QUrl &url) {
        if (!url.isValid() || url.scheme().compare("https", Qt::CaseInsensitive) != 0 || url.host().isEmpty()) {
            status_->setText("Only an absolute HTTPS URL is permitted.");
            return false;
        }
        return true;
    }

    void fetchManifest() {
        if (!rootReady_) {
            status_->setText("Create the root password before using the update fetcher.");
            return;
        }
        const QUrl url(manifestUrl_->text().trimmed());
        if (!validHttps(url))
            return;
        checkButton_->setEnabled(false);
        downloadButton_->setEnabled(false);
        status_->setText("Fetching latest.json using an HTTPS GET request...");
        QNetworkRequest request(url);
        request.setHeader(QNetworkRequest::UserAgentHeader, "HardenedArchUpdateFetcher/1.0");
        QNetworkReply *reply = network_.get(request);
        connect(reply, &QNetworkReply::finished, this, [this, reply] {
            checkButton_->setEnabled(rootReady_);
            if (reply->error() != QNetworkReply::NoError) {
                status_->setText("Manifest request failed: " + reply->errorString());
                reply->deleteLater();
                return;
            }
            QJsonParseError error;
            const QJsonDocument document = QJsonDocument::fromJson(reply->readAll(), &error);
            reply->deleteLater();
            if (error.error != QJsonParseError::NoError || !document.isObject()) {
                status_->setText("latest.json is not valid JSON: " + error.errorString());
                return;
            }
            const QJsonObject root = document.object();
            isoObject_ = root.value("iso").toObject();
            releaseVersion_ = root.value("version").toString();
            const QString filename = isoObject_.value("filename").toString();
            const QString url = isoObject_.value("url").toString();
            const QString sha512 = isoObject_.value("sha512").toString().toLower();
            if (filename.isEmpty() || url.isEmpty() || sha512.size() != 128 || !validHttps(QUrl(url))) {
                status_->setText("Manifest must provide iso.filename, HTTPS iso.url, and a 128-digit iso.sha512 value.");
                isoObject_ = {};
                return;
            }
            append(QString("Release: %1\nFile: %2\nSHA-512: %3\nURL: %4\n\n")
                       .arg(releaseVersion_, filename, sha512, url));
            status_->setText("Manifest accepted. The update can now be downloaded and SHA-512 verified.");
            downloadButton_->setEnabled(true);
        });
    }

    void downloadIso() {
        if (isoObject_.isEmpty())
            return;
        const QUrl url(isoObject_.value("url").toString());
        if (!validHttps(url))
            return;
        const QString filename = QFileInfo(isoObject_.value("filename").toString()).fileName();
        QDir().mkpath("/run/hardened-update");
        finalDownloadPath_ = "/run/hardened-update/" + filename;
        const QString partial = finalDownloadPath_ + ".part";
        downloadFile_.setFileName(partial);
        if (!downloadFile_.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
            status_->setText("Unable to create " + partial);
            return;
        }

        downloadButton_->setEnabled(false);
        checkButton_->setEnabled(false);
        progress_->setValue(0);
        status_->setText("Downloading update using HTTPS GET...");
        QNetworkRequest request(url);
        request.setHeader(QNetworkRequest::UserAgentHeader, "HardenedArchUpdateFetcher/1.0");
        downloadReply_ = network_.get(request);
        connect(downloadReply_, &QNetworkReply::readyRead, this, [this] {
            downloadFile_.write(downloadReply_->readAll());
        });
        connect(downloadReply_, &QNetworkReply::downloadProgress, this,
                [this](qint64 received, qint64 total) {
                    if (total > 0)
                        progress_->setValue(static_cast<int>((received * 100) / total));
                });
        connect(downloadReply_, &QNetworkReply::finished, this, [this, partial] {
            downloadFile_.write(downloadReply_->readAll());
            downloadFile_.close();
            const auto error = downloadReply_->error();
            const QString errorText = downloadReply_->errorString();
            downloadReply_->deleteLater();
            downloadReply_ = nullptr;
            checkButton_->setEnabled(rootReady_);
            if (error != QNetworkReply::NoError) {
                QFile::remove(partial);
                status_->setText("ISO download failed: " + errorText);
                downloadButton_->setEnabled(true);
                return;
            }

            QFile input(partial);
            if (!input.open(QIODevice::ReadOnly)) {
                status_->setText("Downloaded file could not be reopened for verification.");
                downloadButton_->setEnabled(true);
                return;
            }
            QCryptographicHash hash(QCryptographicHash::Sha512);
            while (!input.atEnd())
                hash.addData(input.read(4 * 1024 * 1024));
            input.close();
            const QString actual = QString::fromLatin1(hash.result().toHex()).toLower();
            const QString expected = isoObject_.value("sha512").toString().toLower();
            if (actual != expected) {
                QFile::remove(partial);
                status_->setText("SHA-512 verification FAILED. The downloaded ISO was deleted.");
                append(QString("Expected: %1\nActual:   %2\n").arg(expected, actual));
                downloadButton_->setEnabled(true);
                return;
            }
            QFile::remove(finalDownloadPath_);
            if (!QFile::rename(partial, finalDownloadPath_)) {
                status_->setText("SHA-512 passed, but the verified file could not be renamed.");
                downloadButton_->setEnabled(true);
                return;
            }
            progress_->setValue(100);
            status_->setText("Update ISO downloaded and SHA-512 verified successfully.");
            append("VERIFIED: " + finalDownloadPath_ + "\n");
            downloadButton_->setEnabled(true);
        });
    }
};

int main(int argc, char **argv) {
    QApplication app(argc, argv);
    QString mode = "installer";
    const QStringList args = app.arguments();
    const int index = args.indexOf("--mode");
    if (index >= 0 && index + 1 < args.size())
        mode = args.at(index + 1).trimmed().toLower();
    if (mode != "installer" && mode != "update")
        mode = "installer";
    HardenedWindow window(mode);
    window.showFullScreen();
    return app.exec();
}
'''


CMAKE_SOURCE = r'''cmake_minimum_required(VERSION 3.24)
project(hardened_live_qt LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(Qt6 6.5 REQUIRED COMPONENTS Widgets Network)

qt_add_executable(hardened-live-qt
    main.cpp
)

target_link_libraries(hardened-live-qt PRIVATE
    Qt6::Widgets
    Qt6::Network
)

target_compile_options(hardened-live-qt PRIVATE
    -Wall
    -Wextra
    -Wpedantic
    -Wno-error
)

install(TARGETS hardened-live-qt
    RUNTIME DESTINATION local/bin
)
'''


QT_LAUNCHER = r'''#!/usr/bin/bash
set -u

MODE=${1:-installer}
APP=/usr/local/bin/hardened-live-qt

if [[ ! -x "$APP" ]]; then
    echo "Missing Qt live tool: $APP" >&2
    exit 1
fi

mkdir -p /run/hardened-qt
chmod 0700 /run/hardened-qt
export XDG_RUNTIME_DIR=/run/hardened-qt
export QT_LOGGING_RULES='qt.qpa.*=false'

command -v chvt >/dev/null 2>&1 && chvt 7 || true

if [[ -S /tmp/.X11-unix/X0 ]]; then
    export DISPLAY=:0
    exec "$APP" --mode "$MODE" -platform xcb
fi

for platform in eglfs linuxfb; do
    echo "Trying Qt platform: $platform"
    QT_QPA_PLATFORM="$platform" "$APP" --mode "$MODE" && exit 0
    status=$?
    echo "Qt platform $platform exited with status $status" >&2
done

echo "No usable graphical Qt platform plugin was available." >&2
exit 1
'''


RECOVERY_CONSOLE = r'''#!/usr/bin/bash
set -euo pipefail

exec </dev/tty1 >/dev/tty1 2>&1
clear
printf '%s\n' \
  'Hardened Arch Linux Recovery / Debug Console' \
  'This console never permits passwordless root access.' \
  ''

status=$(/usr/bin/passwd -S root 2>/dev/null | /usr/bin/awk '{print $2}' || true)
if [[ "$status" != "P" ]]; then
    echo 'A root password must be created before recovery access is granted.'
    until /usr/bin/passwd root; do
        echo 'Password creation failed; try again.'
    done
fi

exec /usr/bin/sulogin --force
'''


VERIFY_MEDIA = r'''#!/usr/bin/bash
set -euo pipefail

exec </dev/tty1 >/dev/tty1 2>&1
clear
printf '%s\n' 'Hardened Arch Linux installation-media verification' ''

media=$(/usr/bin/findmnt -rn -t iso9660 -o TARGET | /usr/bin/head -n1 || true)
if [[ -z "$media" ]]; then
    for candidate in /run/hardened-live/media /run/hardened-live/iso /run/archiso/bootmnt; do
        [[ -d "$candidate" ]] && media=$candidate && break
    done
fi

if [[ -z "$media" || ! -d "$media" ]]; then
    echo 'FAILED: the ISO9660 installation medium is not mounted.'
    read -r -p 'Press Enter to reboot.' _
    /usr/bin/systemctl reboot
    exit 1
fi

manifest=$(find "$media" -maxdepth 4 -type f -path '*/hardened/build-manifest.json' -print -quit)
payload=$(find "$media" -maxdepth 4 -type f \( -name 'rootfs.sfs' -o -name '*.squashfs' -o -name '*.sfs' \) -print -quit)

if [[ -z "$manifest" || -z "$payload" ]]; then
    echo 'FAILED: build manifest or SquashFS payload is missing.'
    read -r -p 'Press Enter to reboot.' _
    /usr/bin/systemctl reboot
    exit 1
fi

expected=$(sed -nE 's/.*"rootfs_sha512"[[:space:]]*:[[:space:]]*"([0-9a-fA-F]{128})".*/\1/p' "$manifest" | head -n1 | tr 'A-F' 'a-f')
actual=$(sha512sum "$payload" | awk '{print $1}')

printf 'Payload:  %s\nManifest: %s\n\n' "$payload" "$manifest"
if [[ -n "$expected" && "$actual" == "$expected" ]]; then
    echo 'PASS: the live root payload matches its embedded SHA-512 manifest.'
    status=0
else
    echo 'FAIL: the live root payload does not match its embedded SHA-512 manifest.'
    printf 'Expected: %s\nActual:   %s\n' "${expected:-MISSING}" "$actual"
    status=1
fi

read -r -p 'Press Enter to reboot.' _
/usr/bin/systemctl reboot
exit "$status"
'''


INSTALLER_SERVICE = r'''[Unit]
Description=Hardened Arch Qt Installer
After=systemd-udev-settle.service
Wants=systemd-udev-settle.service
Conflicts=getty@tty7.service

[Service]
Type=simple
ExecStart=/usr/local/sbin/hardened-qt-launch installer
Restart=on-failure
RestartSec=2
StandardInput=tty-force
StandardOutput=journal+console
StandardError=journal+console
TTYPath=/dev/tty7
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes

[Install]
WantedBy=hardened-installer.target
'''


INSTALLER_TARGET = r'''[Unit]
Description=Hardened Arch Qt Installation Mode
Requires=basic.target
After=basic.target
Wants=hardened-installer-qt.service
AllowIsolate=yes
'''


UPDATE_SERVICE = r'''[Unit]
Description=Hardened Arch Qt Software Update Fetcher
After=network-online.target systemd-udev-settle.service
Wants=network-online.target systemd-udev-settle.service
Conflicts=getty@tty7.service

[Service]
Type=simple
ExecStart=/usr/local/sbin/hardened-qt-launch update
Restart=on-failure
RestartSec=2
StandardInput=tty-force
StandardOutput=journal+console
StandardError=journal+console
TTYPath=/dev/tty7
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes

[Install]
WantedBy=hardened-update.target
'''


UPDATE_TARGET = r'''[Unit]
Description=Hardened Arch Qt Update Mode
Requires=basic.target
After=basic.target network-online.target
Wants=network-online.target hardened-update-qt.service
AllowIsolate=yes
'''


RECOVERY_SERVICE = r'''[Unit]
Description=Authenticated Hardened Arch Recovery Console
After=local-fs.target
Conflicts=getty@tty1.service

[Service]
Type=idle
ExecStart=/usr/local/sbin/hardened-recovery-console
StandardInput=tty-force
StandardOutput=tty
StandardError=tty
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes

[Install]
WantedBy=hardened-recovery.target
'''


RECOVERY_TARGET = r'''[Unit]
Description=Hardened Arch Password-Protected Recovery / Debug Mode
Requires=basic.target local-fs.target
After=basic.target local-fs.target
Wants=hardened-recovery.service
AllowIsolate=yes
'''


VERIFY_SERVICE = r'''[Unit]
Description=Verify Hardened Arch Installation Media with SHA-512
After=local-fs.target
Conflicts=getty@tty1.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/hardened-verify-media
StandardInput=tty-force
StandardOutput=tty
StandardError=tty
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes
RemainAfterExit=yes

[Install]
WantedBy=hardened-verify.target
'''


VERIFY_TARGET = r'''[Unit]
Description=Hardened Arch Installation-Media Verification Mode
Requires=basic.target local-fs.target
After=basic.target local-fs.target
Wants=hardened-verify.service
AllowIsolate=yes
'''


GETTY_HARDENED = r'''[Service]
ExecStart=
ExecStart=-/usr/bin/agetty --noclear %I $TERM
'''


SERIAL_GETTY_HARDENED = r'''[Service]
ExecStart=
ExecStart=-/usr/bin/agetty -o '-p -- \\u' --keep-baud 115200,57600,38400,9600 - $TERM
'''


def create_qt_sources(paths: Paths) -> None:
    paths.source_dir.mkdir(parents=True, exist_ok=True)
    write_text(paths.source_dir / "main.cpp", CPP_SOURCE)
    write_text(paths.source_dir / "CMakeLists.txt", CMAKE_SOURCE)


def qt_config_exists(prefix: Path) -> bool:
    candidates = (
        prefix / "lib/cmake/Qt6/Qt6Config.cmake",
        prefix / "lib64/cmake/Qt6/Qt6Config.cmake",
        prefix / "usr/lib/cmake/Qt6/Qt6Config.cmake",
        prefix / "usr/lib64/cmake/Qt6/Qt6Config.cmake",
    )
    return any(path.is_file() for path in candidates)


def find_qt_prefix(explicit: Path | None, runtime_root: Path) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit.expanduser().resolve())
    candidates.extend(DEFAULT_QT_PREFIXES)
    candidates.append(runtime_root / "usr")
    for candidate in candidates:
        if qt_config_exists(candidate):
            print(f"QT PREFIX: {candidate}")
            return candidate
    searched = "\n".join(f"  {item}" for item in candidates)
    raise PatchError(f"Qt6Config.cmake was not found under:\n{searched}")


def build_qt_tool(paths: Paths, qt_prefix: Path, jobs: int) -> None:
    if jobs < 1:
        raise PatchError("--jobs must be at least 1")
    create_qt_sources(paths)
    shutil.rmtree(paths.build_dir, ignore_errors=True)
    shutil.rmtree(paths.install_dir, ignore_errors=True)
    paths.build_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CMAKE_PREFIX_PATH"] = str(qt_prefix)
    run(
        [
            "cmake",
            "-S",
            paths.source_dir,
            "-B",
            paths.build_dir,
            "-G",
            "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_PREFIX_PATH={qt_prefix}",
            "-DCMAKE_INSTALL_PREFIX=/usr",
        ],
        env=env,
    )
    run(["cmake", "--build", paths.build_dir, "--parallel", str(jobs)], env=env)
    install_env = env.copy()
    install_env["DESTDIR"] = str(paths.install_dir)
    run(["cmake", "--install", paths.build_dir], env=install_env)
    if not paths.qt_binary.is_file():
        raise PatchError(f"Qt build did not create {paths.qt_binary}")
    paths.qt_binary.chmod(0o755)


def install_payload_files(paths: Paths, manifest_url: str) -> None:
    root = paths.install_dir
    if not paths.qt_binary.is_file():
        raise PatchError(f"missing Qt binary: {paths.qt_binary}")

    local_bin = root / "usr/local/bin"
    local_sbin = root / "usr/local/sbin"
    unit_dir = root / "usr/lib/systemd/system"
    etc_dir = root / "etc/hardened-arch"
    local_bin.mkdir(parents=True, exist_ok=True)
    local_sbin.mkdir(parents=True, exist_ok=True)
    unit_dir.mkdir(parents=True, exist_ok=True)
    etc_dir.mkdir(parents=True, exist_ok=True)

    for link_name in ("hardened-installer-qt", "hardened-update-qt"):
        link = local_bin / link_name
        link.unlink(missing_ok=True)
        link.symlink_to("hardened-live-qt")

    write_text(local_sbin / "hardened-qt-launch", QT_LAUNCHER, 0o755)
    write_text(local_sbin / "hardened-recovery-console", RECOVERY_CONSOLE, 0o755)
    write_text(local_sbin / "hardened-verify-media", VERIFY_MEDIA, 0o755)

    write_text(unit_dir / "hardened-installer-qt.service", INSTALLER_SERVICE)
    write_text(unit_dir / "hardened-installer.target", INSTALLER_TARGET)
    write_text(unit_dir / "hardened-update-qt.service", UPDATE_SERVICE)
    write_text(unit_dir / "hardened-update.target", UPDATE_TARGET)
    write_text(unit_dir / "hardened-recovery.service", RECOVERY_SERVICE)
    write_text(unit_dir / "hardened-recovery.target", RECOVERY_TARGET)
    write_text(unit_dir / "hardened-verify.service", VERIFY_SERVICE)
    write_text(unit_dir / "hardened-verify.target", VERIFY_TARGET)

    if manifest_url and not manifest_url.lower().startswith("https://"):
        raise PatchError("--manifest-url must use HTTPS")
    write_text(etc_dir / "update.conf", f"MANIFEST_URL={manifest_url}\n", 0o600)

    write_text(
        root / "etc/systemd/system/getty@tty1.service.d/hardened.conf",
        GETTY_HARDENED,
    )
    write_text(
        root / "etc/systemd/system/serial-getty@ttyS0.service.d/hardened.conf",
        SERIAL_GETTY_HARDENED,
    )


def merge_payload_into_runtime(paths: Paths) -> None:
    # copytree(dirs_exist_ok=True, symlinks=True) cannot overwrite an
    # already-existing destination symlink. Remove the two managed launcher
    # aliases first so this finalizer is safe to run repeatedly.
    for relative in (
        "usr/local/bin/hardened-installer-qt",
        "usr/local/bin/hardened-update-qt",
    ):
        destination = paths.runtime_root / relative
        if destination.is_symlink() or destination.is_file():
            destination.unlink()

    for top in ("usr", "etc"):
        source = paths.install_dir / top
        if source.exists():
            shutil.copytree(
                source,
                paths.runtime_root / top,
                dirs_exist_ok=True,
                symlinks=True,
            )
    harden_root_tree(paths.runtime_root)
    patch_live_backends(paths.runtime_root)


def harden_root_tree(root: Path) -> None:
    shadow = root / "etc/shadow"
    if shadow.is_file():
        lines = shadow.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
        output: list[str] = []
        found = False
        for line in lines:
            fields = line.split(":")
            if fields and fields[0] == "root":
                found = True
                if len(fields) < 2:
                    fields.append("!")
                elif fields[1] == "":
                    fields[1] = "!"
                line = ":".join(fields)
            output.append(line)
        if not found:
            raise PatchError(f"root entry missing from {shadow}")
        shadow.write_text("\n".join(output) + "\n", encoding="utf-8", errors="surrogateescape")
        shadow.chmod(0o600)

    systemd_dir = root / "etc/systemd/system"
    if systemd_dir.exists():
        for path in systemd_dir.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                data = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "--autologin root" in data or "agetty --autologin" in data:
                print(f"REMOVING ROOT AUTOLOGIN DROP-IN: {path}")
                path.unlink()

    for path, data in (
        (root / "etc/systemd/system/getty@tty1.service.d/hardened.conf", GETTY_HARDENED),
        (root / "etc/systemd/system/serial-getty@ttyS0.service.d/hardened.conf", SERIAL_GETTY_HARDENED),
    ):
        write_text(path, data)


def patch_live_backends(root: Path) -> None:
    update = root / "usr/local/sbin/hardened-update"
    if update.is_file():
        data = update.read_text(encoding="utf-8", errors="surrogateescape")
        replacements = (
            ("sha256sum", "sha512sum"),
            ("sha256_file", "sha512_file"),
            ("sha256", "sha512"),
            ("SHA-256", "SHA-512"),
            ("SHA256", "SHA512"),
            (".sha256", ".sha512"),
        )
        for old, new in replacements:
            data = data.replace(old, new)
        update.write_text(data, encoding="utf-8", errors="surrogateescape")
        update.chmod(0o755)

    installer = root / "usr/local/sbin/hardened-install"
    if not installer.is_file():
        print(
            "INSTALLER BACKEND PATCH DEFERRED: "
            f"{installer} is not part of the reusable runtime stage. "
            "The ISO builder will patch it after prepare_live_root creates it."
        )
        return
    data = installer.read_text(encoding="utf-8", errors="surrogateescape")
    marker = 'rm -f "$TARGET_MNT/usr/local/sbin/hardened-install"'
    transfer_marker = "HARDENED_ROOT_PASSWORD_TRANSFER"
    transfer_block = r'''
# HARDENED_ROOT_PASSWORD_TRANSFER
if [[ -s /run/hardened-live/root-password.hash && -f "$TARGET_MNT/etc/shadow" ]]; then
    root_hash=$(cat /run/hardened-live/root-password.hash)
    shadow_tmp="$TARGET_MNT/etc/.shadow.hardened-new"
    awk -F: -v OFS=: -v hash="$root_hash" '
        $1 == "root" { $2 = hash }
        { print }
    ' "$TARGET_MNT/etc/shadow" > "$shadow_tmp"
    chmod 0600 "$shadow_tmp"
    chown 0:0 "$shadow_tmp"
    mv -f "$shadow_tmp" "$TARGET_MNT/etc/shadow"
    echo "Installed system root password configured."
else
    echo "ERROR: no authenticated root password was supplied by the Qt installer." >&2
    exit 1
fi
'''.strip("\n")
    if transfer_marker not in data:
        if marker not in data:
            raise PatchError(
                "could not locate the installer cleanup marker needed to transfer the root password"
            )
        data = data.replace(marker, transfer_block + "\n\n" + marker, 1)
        installer.write_text(data, encoding="utf-8", errors="surrogateescape")
        installer.chmod(0o755)
        print(f"PATCHED ROOT PASSWORD TRANSFER: {installer}")


def replace_source_node(source: str, node: ast.AST, replacement: str) -> str:
    if not hasattr(node, "lineno") or not hasattr(node, "end_lineno"):
        raise PatchError("Python AST node has no source range")
    lines = source.splitlines(keepends=True)
    start = node.lineno - 1
    end = node.end_lineno
    new_lines = replacement.rstrip() + "\n"
    return "".join(lines[:start]) + new_lines + "".join(lines[end:])


def sanitize_live_init(value: str) -> str:
    lines = value.splitlines()
    output: list[str] = []
    skipping_rd_shell = False
    for line in lines:
        stripped = line.strip()
        if skipping_rd_shell:
            if stripped == ";;":
                skipping_rd_shell = False
            continue
        if re.search(r"\brd\.shell(?:=1)?\b", line):
            if stripped.endswith(")") or ")" in stripped:
                skipping_rd_shell = True
            continue
        if "early-init.log" in line:
            continue
        if re.match(r"^\s*LOG=", line):
            continue
        if "$LOG" in line:
            continue
        if re.search(r"(?:^|[;&|\s])logger(?:\s|$)", line):
            continue
        if "set -x" in stripped:
            continue
        if re.search(r"\bexec\s+(?:/usr/bin/|/bin/)?(?:ba)?sh(?:\s|$)", line):
            indent = line[: len(line) - len(line.lstrip())]
            output.extend(
                [
                    indent + 'echo "Early boot failed; no unrestricted initramfs shell is available." >&2',
                    indent + "sleep 8",
                    indent + "reboot -f",
                ]
            )
            continue
        output.append(line)

    text = "\n".join(output).rstrip() + "\n"
    if text.startswith("#!"):
        first, rest = text.split("\n", 1)
        text = first + "\numask 077\n" + rest
    if "early-init.log" in text or "rd.shell" in text:
        raise PatchError("failed to remove the diagnostic logger or rd.shell from LIVE_INIT")
    if re.search(r"\bexec\s+(?:/usr/bin/|/bin/)?(?:ba)?sh(?:\s|$)", text):
        raise PatchError("an unrestricted early-init shell remains")
    return text


NEW_LIMINE_FUNCTION = r'''def limine_config(kver: str, label: str) -> str:
    kernel_path = f"boot():/EFI/Linux/vmlinuz-{kver}.efi"
    initrd_path = f"boot():/EFI/Linux/initramfs-live-{kver}.img.zst"
    common = f"iso_label={label} rootfstype=btrfs rd.shell=0"
    return f"""timeout: 8
editor_enabled: no
graphics: yes
wallpaper: boot():/EFI/BOOT/limine-bg.png

/Hardened Arch Linux
    protocol: linux
    path: {kernel_path}
    module_path: {initrd_path}
    cmdline: {common} hardened.mode=live systemd.unit=graphical.target quiet loglevel=3 systemd.show_status=auto

/Install Hardened Arch Linux (Qt)
    protocol: linux
    path: {kernel_path}
    module_path: {initrd_path}
    cmdline: {common} hardened.mode=install systemd.unit=hardened-installer.target loglevel=4 systemd.show_status=yes

/Software Update Fetcher (Qt)
    protocol: linux
    path: {kernel_path}
    module_path: {initrd_path}
    cmdline: {common} hardened.mode=update systemd.unit=hardened-update.target loglevel=4 systemd.show_status=yes

/Verify Installation Media (SHA-512)
    protocol: linux
    path: {kernel_path}
    module_path: {initrd_path}
    cmdline: {common} hardened.mode=verify systemd.unit=hardened-verify.target loglevel=4 systemd.show_status=yes

/Recovery / Debug Console (password required)
    protocol: linux
    path: {kernel_path}
    module_path: {initrd_path}
    cmdline: {common} hardened.mode=recovery systemd.unit=hardened-recovery.target loglevel=7 systemd.log_level=debug systemd.show_status=yes
"""
'''


BUILDER_HELPERS = r"""

HARDENED_QT_PAYLOAD_ROOT = Path("/home/corbett/hardened-qt-tools/install")


def _harden_root_account_in_tree(root: Path) -> None:
    shadow = root / "etc/shadow"
    if shadow.is_file():
        lines = shadow.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
        rewritten = []
        found = False
        for line in lines:
            fields = line.split(":")
            if fields and fields[0] == "root":
                found = True
                if len(fields) < 2:
                    fields.append("!")
                elif fields[1] == "":
                    fields[1] = "!"
                line = ":".join(fields)
            rewritten.append(line)
        if not found:
            raise BuildError(f"root account is missing from {shadow}")
        shadow.write_text("\n".join(rewritten) + "\n", encoding="utf-8", errors="surrogateescape")
        shadow.chmod(0o600)

    systemd_dir = root / "etc/systemd/system"
    if systemd_dir.exists():
        for path in systemd_dir.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            data = path.read_text(encoding="utf-8", errors="ignore")
            if "--autologin root" in data or "agetty --autologin" in data:
                path.unlink()


def _patch_hardened_live_backends(root: Path) -> None:
    update = root / "usr/local/sbin/hardened-update"
    if update.is_file():
        data = update.read_text(encoding="utf-8", errors="surrogateescape")
        for old, new in (
            ("sha256sum", "sha512sum"),
            ("sha256_file", "sha512_file"),
            ("sha256", "sha512"),
            ("SHA-256", "SHA-512"),
            ("SHA256", "SHA512"),
            (".sha256", ".sha512"),
        ):
            data = data.replace(old, new)
        update.write_text(data, encoding="utf-8", errors="surrogateescape")
        update.chmod(0o755)

    installer = root / "usr/local/sbin/hardened-install"
    if not installer.is_file():
        raise BuildError(f"installer backend is missing: {installer}")
    data = installer.read_text(encoding="utf-8", errors="surrogateescape")
    marker = 'rm -f "$TARGET_MNT/usr/local/sbin/hardened-install"'
    transfer_marker = "HARDENED_ROOT_PASSWORD_TRANSFER"
    transfer_block = r'''# HARDENED_ROOT_PASSWORD_TRANSFER
if [[ -s /run/hardened-live/root-password.hash && -f "$TARGET_MNT/etc/shadow" ]]; then
    root_hash=$(cat /run/hardened-live/root-password.hash)
    shadow_tmp="$TARGET_MNT/etc/.shadow.hardened-new"
    awk -F: -v OFS=: -v hash="$root_hash" '
        $1 == "root" { $2 = hash }
        { print }
    ' "$TARGET_MNT/etc/shadow" > "$shadow_tmp"
    chmod 0600 "$shadow_tmp"
    chown 0:0 "$shadow_tmp"
    mv -f "$shadow_tmp" "$TARGET_MNT/etc/shadow"
    echo "Installed system root password configured."
else
    echo "ERROR: no authenticated root password was supplied by the Qt installer." >&2
    exit 1
fi'''
    if transfer_marker not in data:
        if marker not in data:
            raise BuildError("could not locate installer cleanup marker for root-password transfer")
        data = data.replace(marker, transfer_block + "\n\n" + marker, 1)
        installer.write_text(data, encoding="utf-8", errors="surrogateescape")
        installer.chmod(0o755)


def install_hardened_qt_security_payload(live_root: Path, cfg: BuildConfig) -> None:
    payload = HARDENED_QT_PAYLOAD_ROOT
    if not (payload / "usr/local/bin/hardened-live-qt").is_file():
        raise BuildError(f"missing prebuilt Qt payload: {payload}")

    # Make repeated --keep-work ISO builds idempotent. shutil.copytree cannot
    # replace an existing symlink even with dirs_exist_ok=True.
    for relative in (
        "usr/local/bin/hardened-installer-qt",
        "usr/local/bin/hardened-update-qt",
    ):
        destination = live_root / relative
        if destination.is_symlink() or destination.is_file():
            destination.unlink()

    for top in ("usr", "etc"):
        source = payload / top
        if source.exists():
            shutil.copytree(source, live_root / top, dirs_exist_ok=True, symlinks=True)

    update_url = cfg.repo_url or ""
    if update_url and not update_url.lower().startswith("https://"):
        raise BuildError("the update manifest URL must use HTTPS")
    write_text(
        live_root / "etc/hardened-arch/update.conf",
        f"MANIFEST_URL={update_url}\n",
        mode=0o600,
    )
    _harden_root_account_in_tree(live_root)
    _patch_hardened_live_backends(live_root)


def verify_iso_sha512(output: Path) -> None:
    sidecar = output.with_suffix(output.suffix + ".sha512")
    if not sidecar.is_file():
        raise BuildError(f"missing SHA-512 sidecar: {sidecar}")
    fields = sidecar.read_text(encoding="utf-8").strip().split()
    if not fields or len(fields[0]) != 128:
        raise BuildError(f"invalid SHA-512 sidecar: {sidecar}")
    expected = fields[0].lower()
    actual = sha512_file(output).lower()
    if expected != actual:
        raise BuildError(
            f"ISO SHA-512 verification failed: expected {expected}, got {actual}"
        )
    print(f"ISO SHA-512 VERIFIED: {output}")
"""


def ensure_import(source: str, module: str) -> str:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Import) and any(alias.name == module for alias in node.names):
            return source
        if isinstance(node, ast.ImportFrom) and node.module == module:
            return source

    lines = source.splitlines(keepends=True)
    insert_line = 0
    body = list(tree.body)
    index = 0
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        insert_line = body[0].end_lineno
        index = 1
    while index < len(body):
        node = body[index]
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            insert_line = node.end_lineno
            index += 1
            continue
        break
    lines.insert(insert_line, f"import {module}\n")
    return "".join(lines)


def apply_sha512_conversion(source: str) -> str:
    replacements = (
        ("sha256_file", "sha512_file"),
        ("hashlib.sha256", "hashlib.sha512"),
        ("sha256sum", "sha512sum"),
        ("rootfs_sha256", "rootfs_sha512"),
        ("\"sha256\"", "\"sha512\""),
        ("'sha256'", "'sha512'"),
        (".sha256", ".sha512"),
        ("SHA-256", "SHA-512"),
        ("SHA256", "SHA512"),
    )
    for old, new in replacements:
        source = source.replace(old, new)
    return source


def patch_builder(paths: Paths) -> None:
    if not paths.builder.is_file():
        raise PatchError(f"builder not found: {paths.builder}")
    backup_once(paths.builder, ".before-qt-security-sha512.bak")
    source = paths.builder.read_text(encoding="utf-8")
    source = ensure_import(source, "shutil")
    source = apply_sha512_conversion(source)

    tree = ast.parse(source)
    replacements: list[tuple[int, int, str]] = []
    live_init_found = False
    limine_found = False

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
                value_node = node.value
            else:
                targets = [node.target]
                value_node = node.value
            if any(isinstance(target, ast.Name) and target.id == "LIVE_INIT" for target in targets):
                if not isinstance(value_node, ast.Constant) or not isinstance(value_node.value, str):
                    raise PatchError("LIVE_INIT is not a literal string")
                sanitized = sanitize_live_init(value_node.value)
                replacement = "LIVE_INIT = " + repr(sanitized)
                replacements.append((node.lineno, node.end_lineno, replacement))
                live_init_found = True
        if isinstance(node, ast.FunctionDef) and node.name == "limine_config":
            replacements.append((node.lineno, node.end_lineno, NEW_LIMINE_FUNCTION.rstrip()))
            limine_found = True

    if not live_init_found:
        raise PatchError("could not locate LIVE_INIT in the builder")
    if not limine_found:
        raise PatchError("could not locate limine_config in the builder")

    lines = source.splitlines(keepends=True)
    for start, end, replacement in sorted(replacements, reverse=True):
        lines[start - 1 : end] = [replacement + "\n"]
    source = "".join(lines)

    if "def install_hardened_qt_security_payload(" not in source:
        main_match = re.search(r"^def main\s*\(", source, flags=re.MULTILINE)
        if not main_match:
            raise PatchError("could not locate main() in the builder")
        helper_text = BUILDER_HELPERS.replace(
            "/home/corbett/hardened-qt-tools/install",
            str(paths.install_dir),
        )
        source = source[: main_match.start()] + helper_text + "\n\n" + source[main_match.start() :]

    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    insertions: list[tuple[int, str]] = []
    payload_call_present = "install_hardened_qt_security_payload(paths.live_root, cfg)" in source
    verify_call_present = "verify_iso_sha512(paths.output)" in source

    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        func_name = func.id if isinstance(func, ast.Name) else ""
        indent = " " * node.col_offset
        if func_name == "prepare_live_root" and not payload_call_present:
            insertions.append(
                (node.end_lineno, indent + "install_hardened_qt_security_payload(paths.live_root, cfg)\n")
            )
            payload_call_present = True
        if func_name == "build_iso" and not verify_call_present:
            insertions.append((node.end_lineno, indent + "verify_iso_sha512(paths.output)\n"))
            verify_call_present = True

    if not payload_call_present:
        raise PatchError("could not insert the Qt/security payload call after prepare_live_root")
    if not verify_call_present:
        raise PatchError("could not insert ISO SHA-512 verification after build_iso")

    for line_number, text in sorted(insertions, reverse=True):
        lines.insert(line_number, text)
    source = "".join(lines)

    paths.builder.write_text(source, encoding="utf-8")
    paths.builder.chmod(0o755)
    py_compile.compile(str(paths.builder), doraise=True)

    final = paths.builder.read_text(encoding="utf-8")
    required = (
        "editor_enabled: no",
        "hardened-installer.target",
        "hardened-update.target",
        "hardened-recovery.target",
        "hardened-verify.target",
        "rootfs_sha512",
        ".sha512",
        "verify_iso_sha512(paths.output)",
        "install_hardened_qt_security_payload(paths.live_root, cfg)",
    )
    missing = [item for item in required if item not in final]
    if missing:
        raise PatchError("builder verification failed; missing: " + ", ".join(missing))
    if "early-init.log" in final:
        raise PatchError("early-init diagnostic file logger still exists in the builder")
    print(f"BUILDER PATCHED AND SYNTAX CHECKED: {paths.builder}")


def patch_repair_script(builder_parent: Path) -> None:
    repair = builder_parent / "repair_hardened_arch_physical_boot.py"
    if not repair.is_file():
        return
    backup_once(repair, ".before-no-early-log.bak")
    data = repair.read_text(encoding="utf-8")
    data = re.sub(r"^.*early-init\.log.*\n?", "", data, flags=re.MULTILINE)
    data = re.sub(r"^.*\$LOG.*\n?", "", data, flags=re.MULTILINE)
    data = data.replace("rd.shell|rd.shell=1", "rd.shell-disabled")
    repair.write_text(data, encoding="utf-8")
    try:
        py_compile.compile(str(repair), doraise=True)
    except py_compile.PyCompileError:
        shutil.copy2(repair.with_name(repair.name + ".before-no-early-log.bak"), repair)
        print("NOTICE: left the old physical-boot repair script unchanged because a safe syntax-preserving edit was not possible.")
    else:
        print(f"OLD EARLY-LOGGER PATCHER NEUTERED: {repair}")


def validate_runtime(paths: Paths) -> None:
    checks = (
        paths.runtime_root / "usr/local/bin/hardened-live-qt",
        paths.runtime_root / "usr/local/sbin/hardened-qt-launch",
        paths.runtime_root / "usr/local/sbin/hardened-recovery-console",
        paths.runtime_root / "usr/local/sbin/hardened-verify-media",
        paths.runtime_root / "usr/lib/systemd/system/hardened-installer.target",
        paths.runtime_root / "usr/lib/systemd/system/hardened-update.target",
        paths.runtime_root / "usr/lib/systemd/system/hardened-recovery.target",
        paths.runtime_root / "usr/lib/systemd/system/hardened-verify.target",
    )
    missing = [str(path) for path in checks if not path.exists()]
    if missing:
        raise PatchError("runtime payload verification failed:\n" + "\n".join(missing))

    installer = paths.runtime_root / "usr/local/sbin/hardened-install"
    if installer.is_file():
        if "HARDENED_ROOT_PASSWORD_TRANSFER" not in installer.read_text(
            encoding="utf-8", errors="ignore"
        ):
            raise PatchError("installer root-password transfer patch is missing")
    else:
        print(
            "INSTALLER BACKEND VALIDATION DEFERRED: "
            "the reusable runtime stage does not contain hardened-install; "
            "the patched ISO builder validates and patches it in the live root."
        )
    print(f"RUNTIME PAYLOAD VERIFIED: {paths.runtime_root}")


def main() -> int:
    args = parse_args()
    paths = make_paths(args)
    require_root()

    if not paths.runtime_root.is_dir():
        raise PatchError(f"runtime root not found: {paths.runtime_root}")

    if args.skip_qt_build:
        if not paths.qt_binary.is_file():
            raise PatchError(f"--skip-qt-build requested but {paths.qt_binary} is missing")
    else:
        qt_prefix = find_qt_prefix(args.qt_prefix, paths.runtime_root)
        build_qt_tool(paths, qt_prefix, args.jobs)

    install_payload_files(paths, args.manifest_url)
    merge_payload_into_runtime(paths)
    patch_builder(paths)
    patch_repair_script(paths.builder.parent)
    validate_runtime(paths)

    print("\nHARDENED ISO FINALIZATION PATCH COMPLETE")
    print(f"Qt source:     {paths.source_dir}")
    print(f"Qt binary:     {paths.qt_binary}")
    print(f"Runtime root:  {paths.runtime_root}")
    print(f"ISO builder:   {paths.builder}")
    print("Checksums:     SHA-512 sidecar plus immediate post-build verification")
    print("Root access:   password required; no initramfs shell; no root autologin")
    print("Limine modes:  live, Qt install, Qt update, SHA-512 verify, authenticated recovery")
    print("\nBuild the ISO directly with your normal builder command.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
