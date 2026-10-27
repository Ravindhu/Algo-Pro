"""Regenerate broker_login_task.xml as valid, UTF-16LE Task Scheduler XML.

WHY A GENERATOR
===============
Task Scheduler only accepts UTF-16, and XML forbids "--" inside comments, so the
hand-written template was never importable: it had a "----" rule inside the
comment block. Keeping the XML in a function here means it is always written with
the declared encoding and a comment body that cannot break the parser.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET = REPO_ROOT / "scripts" / "ops" / "broker_login_task.xml"

#: Kept short on purpose: every line here must stay legal XML comment text, so
#: no "--" anywhere, and no "--" in any sentence. The long rationale lives in
#: scripts/ops/scheduled_login.py, which is where an operator will look anyway.
TEMPLATE = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <!--
    Headless OpenAlgo + broker TOTP login, weekdays 08:50, 25 min before the open.
    Install:  schtasks /Create /TN "BlueEngine\\BrokerLogin" /XML THIS_FILE /F

    RestartOnFailure is 1 attempt after 15 minutes, not 3 in 5 minutes: a broker
    locks an account for repeated logins, and OpenAlgo rate limits /auth/login to
    5 per minute and 25 per hour per IP. The script also enforces its own
    cooldown (10 min after a success, 30 min after a failure), so a bad morning
    is two attempts at most.

    A boot trigger is deliberately absent: a boot-time login can land hours
    before the open and expire before it matters.
  -->
  <RegistrationInfo>
    <Description>Blue Engine: headless OpenAlgo + broker TOTP login before the Indian market open. Never trades.</Description>
    <URI>\\BlueEngine\\BrokerLogin</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-10-27T08:50:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <DaysOfWeek>
          <Monday />
          <Tuesday />
          <Wednesday />
          <Thursday />
          <Friday />
        </DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT15M</Interval>
      <Count>1</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>__REPO_ROOT__\\scripts\\ops\\SCHEDULED_LOGIN.cmd</Command>
      <WorkingDirectory>__REPO_ROOT__</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def main() -> int:
    # Parse BEFORE writing: a malformed template must never reach Task Scheduler.
    import xml.etree.ElementTree as ET

    ET.fromstring(TEMPLATE.encode("utf-16"))
    TARGET.write_bytes(TEMPLATE.encode("utf-16"))   # BOM + LE, as declared
    sys.stdout.write(f"wrote {TARGET} ({TARGET.stat().st_size} bytes, utf-16)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
