local target, accepted, watching, pending
local screenshotFormat

local function confirm()
    if pending and not screenshotFormat then
        -- A TGA file signals an in-game acknowledgment to the viewer process.
        screenshotFormat = GetCVar("screenshotFormat")
        SetCVar("screenshotFormat", "tga")
        Screenshot()
    end
end

SLASH_AGENTWOWWATCH1 = "/agentwowwatch"
SlashCmdList.AGENTWOWWATCH = function(name)
    if not name:match("^[A-Za-z]+$") or #name < 2 or #name > 12 then
        return
    end
    name = name:lower()
    if target ~= name then
        target, accepted, watching = name, false, false
    end
    pending = true
    if watching then
        confirm()
    elseif not accepted then
        SendChatMessage(".watch " .. target, "SAY")
    end
end

local frame = CreateFrame("Frame")
frame:RegisterEvent("CHAT_MSG_SYSTEM")
frame:RegisterEvent("SCREENSHOT_SUCCEEDED")
frame:RegisterEvent("SCREENSHOT_FAILED")
frame:SetScript("OnEvent", function(_, event, message)
    if event ~= "CHAT_MSG_SYSTEM" then
        if screenshotFormat then
            SetCVar("screenshotFormat", screenshotFormat)
            screenshotFormat = nil
            pending = event ~= "SCREENSHOT_SUCCEEDED"
        end
    elseif target then
        local text = message:lower()
        if text == "watching " .. target .. ". use .unwatch before logging out."
            or text == "watch target changed to " .. target .. "." then
            accepted, watching = true, false
        elseif accepted and text == "watching the target. use .unwatch to stop." then
            watching = true
            confirm()
        elseif text == "watch ended; your settings have been restored." then
            accepted, watching = false, false
        elseif text:match("^following the target;") or text:match("^waiting for target ")
            or text:match("^watch paused:") then
            watching = false
        end
    end
end)
