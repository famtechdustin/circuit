<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE eagle SYSTEM "eagle.dtd">
<eagle version="7.7.0">
  <drawing>
    <settings>
      <setting alwaysvectorfont="no" />
    </settings>
    <grid distance="0.1" unitdist="inch" unit="inch" style="lines" multiple="1" display="no" altdistance="0.01" altunitdist="inch" altunit="inch" />
    <layers>
      <layer number="1" name="Top" color="4" fill="1" visible="yes" active="yes" />
      <layer number="16" name="Bottom" color="1" fill="1" visible="yes" active="yes" />
      <layer number="17" name="Pads" color="2" fill="1" visible="yes" active="yes" />
      <layer number="18" name="Vias" color="2" fill="1" visible="yes" active="yes" />
      <layer number="20" name="Dimension" color="15" fill="1" visible="yes" active="yes" />
      <layer number="21" name="tPlace" color="7" fill="1" visible="yes" active="yes" />
      <layer number="25" name="tNames" color="7" fill="1" visible="yes" active="yes" />
      <layer number="27" name="tValues" color="7" fill="1" visible="yes" active="yes" />
      <layer number="51" name="tDocu" color="7" fill="1" visible="yes" active="yes" />
      <layer number="91" name="Nets" color="2" fill="1" visible="yes" active="yes" />
      <layer number="92" name="Busses" color="1" fill="1" visible="yes" active="yes" />
      <layer number="94" name="Symbols" color="4" fill="1" visible="yes" active="yes" />
      <layer number="95" name="Names" color="7" fill="1" visible="yes" active="yes" />
      <layer number="96" name="Values" color="7" fill="1" visible="yes" active="yes" />
      <layer number="97" name="Info" color="7" fill="1" visible="yes" active="yes" />
    </layers>
    <schematic>
      <libraries>
        <library name="circuit">
          <packages>
            <package name="PKG_R3">
              <smd name="1" x="0" y="0" dx="1.5" dy="1.5" layer="1" />
              <smd name="2" x="2.54" y="0" dx="1.5" dy="1.5" layer="1" />
              <text x="0" y="2.54" size="1.27" layer="25">&gt;NAME</text>
              <text x="0" y="-2.54" size="1.27" layer="27">&gt;VALUE</text>
            </package>
            <package name="PKG_C17">
              <smd name="1" x="0" y="0" dx="1.5" dy="1.5" layer="1" />
              <smd name="2" x="2.54" y="0" dx="1.5" dy="1.5" layer="1" />
              <text x="0" y="2.54" size="1.27" layer="25">&gt;NAME</text>
              <text x="0" y="-2.54" size="1.27" layer="27">&gt;VALUE</text>
            </package>
            <package name="PKG_D1">
              <smd name="1" x="0" y="0" dx="1.5" dy="1.5" layer="1" />
              <smd name="2" x="2.54" y="0" dx="1.5" dy="1.5" layer="1" />
              <text x="0" y="2.54" size="1.27" layer="25">&gt;NAME</text>
              <text x="0" y="-2.54" size="1.27" layer="27">&gt;VALUE</text>
            </package>
            <package name="PKG_Q1">
              <smd name="1" x="0" y="0" dx="1.5" dy="1.5" layer="1" />
              <smd name="2" x="2.54" y="0" dx="1.5" dy="1.5" layer="1" />
              <smd name="3" x="5.08" y="0" dx="1.5" dy="1.5" layer="1" />
              <text x="0" y="2.54" size="1.27" layer="25">&gt;NAME</text>
              <text x="0" y="-2.54" size="1.27" layer="27">&gt;VALUE</text>
            </package>
            <package name="PKG_U1">
              <smd name="1" x="0" y="0" dx="1.5" dy="1.5" layer="1" />
              <smd name="2" x="2.54" y="0" dx="1.5" dy="1.5" layer="1" />
              <smd name="3" x="5.08" y="0" dx="1.5" dy="1.5" layer="1" />
              <text x="0" y="2.54" size="1.27" layer="25">&gt;NAME</text>
              <text x="0" y="-2.54" size="1.27" layer="27">&gt;VALUE</text>
            </package>
          </packages>
          <symbols>
            <symbol name="RESISTOR_R3">
              <wire x1="0" y1="-5.08" x2="1.778" y2="-5.08" width="0.254" layer="94" />
              <wire x1="8.382" y1="-5.08" x2="10.16" y2="-5.08" width="0.254" layer="94" />
              <wire x1="1.778" y1="-5.08" x2="2.8787" y2="-2.6416" width="0.254" layer="94" />
              <wire x1="2.8787" y1="-2.6416" x2="3.9793" y2="-7.5184" width="0.254" layer="94" />
              <wire x1="3.9793" y1="-7.5184" x2="5.08" y2="-2.6416" width="0.254" layer="94" />
              <wire x1="5.08" y1="-2.6416" x2="6.1807" y2="-7.5184" width="0.254" layer="94" />
              <wire x1="6.1807" y1="-7.5184" x2="7.2813" y2="-2.6416" width="0.254" layer="94" />
              <wire x1="7.2813" y1="-2.6416" x2="8.382" y2="-5.08" width="0.254" layer="94" />
              <text x="0" y="1.27" size="1.778" layer="95">&gt;NAME</text>
              <text x="0" y="-10.668" size="1.778" layer="96">&gt;VALUE</text>
              <pin name="A" x="-2.54" y="-5.08" length="short" direction="pas" rot="R0" />
              <pin name="B" x="12.7" y="-5.08" length="short" direction="pas" rot="R180" />
            </symbol>
            <symbol name="CAPACITOR_C17">
              <wire x1="0" y1="-5.08" x2="3.8608" y2="-5.08" width="0.254" layer="94" />
              <wire x1="6.2992" y1="-5.08" x2="10.16" y2="-5.08" width="0.254" layer="94" />
              <wire x1="3.8608" y1="-7.747" x2="3.8608" y2="-2.413" width="0.4064" layer="94" />
              <wire x1="6.2992" y1="-7.747" x2="6.2992" y2="-2.413" width="0.4064" layer="94" />
              <text x="0" y="1.27" size="1.778" layer="95">&gt;NAME</text>
              <text x="0" y="-10.668" size="1.778" layer="96">&gt;VALUE</text>
              <pin name="1" x="-2.54" y="-5.08" length="short" direction="pas" rot="R0" />
              <pin name="2" x="12.7" y="-5.08" length="short" direction="pas" rot="R180" />
            </symbol>
            <symbol name="DIODE_D1">
              <wire x1="0" y1="-5.08" x2="2.8448" y2="-5.08" width="0.254" layer="94" />
              <wire x1="6.9088" y1="-5.08" x2="10.16" y2="-5.08" width="0.254" layer="94" />
              <wire x1="2.8448" y1="-7.747" x2="2.8448" y2="-2.413" width="0.254" layer="94" />
              <wire x1="2.8448" y1="-7.747" x2="6.9088" y2="-5.08" width="0.254" layer="94" />
              <wire x1="2.8448" y1="-2.413" x2="6.9088" y2="-5.08" width="0.254" layer="94" />
              <wire x1="6.9088" y1="-7.747" x2="6.9088" y2="-2.413" width="0.4064" layer="94" />
              <text x="0" y="1.27" size="1.778" layer="95">&gt;NAME</text>
              <text x="0" y="-10.668" size="1.778" layer="96">&gt;VALUE</text>
              <pin name="A" x="-2.54" y="-5.08" length="short" direction="pas" rot="R0" />
              <pin name="K" x="12.7" y="-5.08" length="short" direction="pas" rot="R180" />
            </symbol>
            <symbol name="NMOS_Q1">
              <wire x1="0" y1="-8.89" x2="5.174" y2="-8.89" width="0.254" layer="94" />
              <wire x1="5.174" y1="-12.606" x2="5.174" y2="-5.174" width="0.254" layer="94" />
              <wire x1="8.5522" y1="-12.606" x2="8.5522" y2="-10.3764" width="0.4064" layer="94" />
              <wire x1="8.5522" y1="-9.7008" x2="8.5522" y2="-8.0792" width="0.4064" layer="94" />
              <wire x1="8.5522" y1="-7.4036" x2="8.5522" y2="-5.174" width="0.4064" layer="94" />
              <wire x1="8.5522" y1="-5.174" x2="11.9304" y2="-5.174" width="0.254" layer="94" />
              <wire x1="11.9304" y1="-5.174" x2="11.9304" y2="-3.1471" width="0.254" layer="94" />
              <wire x1="8.5522" y1="-12.606" x2="11.9304" y2="-12.606" width="0.254" layer="94" />
              <wire x1="11.9304" y1="-12.606" x2="11.9304" y2="-14.6329" width="0.254" layer="94" />
              <wire x1="11.9304" y1="-12.606" x2="8.6792" y2="-12.606" width="0.254" layer="94" />
              <wire x1="8.6792" y1="-12.606" x2="9.4412" y2="-11.971" width="0.254" layer="94" />
              <wire x1="8.6792" y1="-12.606" x2="9.4412" y2="-13.241" width="0.254" layer="94" />
              <wire x1="11.9304" y1="-5.174" x2="13.6195" y2="-5.174" width="0.254" layer="94" />
              <wire x1="13.6195" y1="-5.174" x2="13.6195" y2="-12.606" width="0.254" layer="94" />
              <wire x1="13.6195" y1="-12.606" x2="11.9304" y2="-12.606" width="0.254" layer="94" />
              <wire x1="13.1115" y1="-9.271" x2="14.1275" y2="-9.271" width="0.254" layer="94" />
              <wire x1="13.1115" y1="-9.271" x2="13.6195" y2="-8.382" width="0.254" layer="94" />
              <wire x1="14.1275" y1="-9.271" x2="13.6195" y2="-8.382" width="0.254" layer="94" />
              <wire x1="13.1115" y1="-8.382" x2="14.1275" y2="-8.382" width="0.4064" layer="94" />
              <text x="0" y="1.27" size="1.778" layer="95">&gt;NAME</text>
              <text x="0" y="-20.828" size="1.778" layer="96">&gt;VALUE</text>
              <pin name="G" x="-2.54" y="-10.16" length="short" direction="in" rot="R0" />
              <pin name="D" x="12.7" y="2.54" length="short" direction="pas" rot="R270" />
              <pin name="S" x="12.7" y="-20.32" length="short" direction="pas" rot="R90" />
            </symbol>
            <symbol name="MCU_U1">
              <wire x1="0" y1="0" x2="30.48" y2="0" width="0.254" layer="94" />
              <wire x1="30.48" y1="0" x2="30.48" y2="-22.86" width="0.254" layer="94" />
              <wire x1="30.48" y1="-22.86" x2="0" y2="-22.86" width="0.254" layer="94" />
              <wire x1="0" y1="-22.86" x2="0" y2="0" width="0.254" layer="94" />
              <text x="0" y="1.27" size="1.778" layer="95">&gt;NAME</text>
              <text x="0" y="-25.908" size="1.778" layer="96">&gt;VALUE</text>
              <pin name="VCC" x="-2.54" y="-7.62" length="short" direction="pas" rot="R0" />
              <pin name="GND" x="-2.54" y="-15.24" length="short" direction="pas" rot="R0" />
              <pin name="IO" x="33.02" y="-10.16" length="short" direction="pas" rot="R180" />
            </symbol>
          </symbols>
          <devicesets>
            <deviceset name="RESISTOR_R3" prefix="R" uservalue="yes">
              <description />
              <gates>
                <gate name="G$1" symbol="RESISTOR_R3" x="0" y="0" />
              </gates>
              <devices>
                <device name="" package="PKG_R3">
                  <connects>
                    <connect gate="G$1" pin="A" pad="1" />
                    <connect gate="G$1" pin="B" pad="2" />
                  </connects>
                  <technologies>
                    <technology name="" />
                  </technologies>
                </device>
              </devices>
            </deviceset>
            <deviceset name="CAPACITOR_C17" prefix="C" uservalue="yes">
              <description />
              <gates>
                <gate name="G$1" symbol="CAPACITOR_C17" x="0" y="0" />
              </gates>
              <devices>
                <device name="" package="PKG_C17">
                  <connects>
                    <connect gate="G$1" pin="1" pad="1" />
                    <connect gate="G$1" pin="2" pad="2" />
                  </connects>
                  <technologies>
                    <technology name="" />
                  </technologies>
                </device>
              </devices>
            </deviceset>
            <deviceset name="DIODE_D1" prefix="D" uservalue="yes">
              <description />
              <gates>
                <gate name="G$1" symbol="DIODE_D1" x="0" y="0" />
              </gates>
              <devices>
                <device name="" package="PKG_D1">
                  <connects>
                    <connect gate="G$1" pin="A" pad="1" />
                    <connect gate="G$1" pin="K" pad="2" />
                  </connects>
                  <technologies>
                    <technology name="" />
                  </technologies>
                </device>
              </devices>
            </deviceset>
            <deviceset name="NMOS_Q1" prefix="Q" uservalue="yes">
              <description />
              <gates>
                <gate name="G$1" symbol="NMOS_Q1" x="0" y="0" />
              </gates>
              <devices>
                <device name="" package="PKG_Q1">
                  <connects>
                    <connect gate="G$1" pin="G" pad="1" />
                    <connect gate="G$1" pin="D" pad="2" />
                    <connect gate="G$1" pin="S" pad="3" />
                  </connects>
                  <technologies>
                    <technology name="" />
                  </technologies>
                </device>
              </devices>
            </deviceset>
            <deviceset name="MCU_U1" prefix="U" uservalue="yes">
              <description>MCU</description>
              <gates>
                <gate name="G$1" symbol="MCU_U1" x="0" y="0" />
              </gates>
              <devices>
                <device name="" package="PKG_U1">
                  <connects>
                    <connect gate="G$1" pin="VCC" pad="1" />
                    <connect gate="G$1" pin="GND" pad="2" />
                    <connect gate="G$1" pin="IO" pad="3" />
                  </connects>
                  <technologies>
                    <technology name="" />
                  </technologies>
                </device>
              </devices>
            </deviceset>
          </devicesets>
        </library>
      </libraries>
      <attributes />
      <variantdefs />
      <classes>
        <class number="0" name="default" width="0" drill="0" />
      </classes>
      <parts>
        <part name="R3" library="circuit" deviceset="RESISTOR_R3" device="" value="10k" />
        <part name="C17" library="circuit" deviceset="CAPACITOR_C17" device="" value="3nF" />
        <part name="D1" library="circuit" deviceset="DIODE_D1" device="" value="1N4148" />
        <part name="Q1" library="circuit" deviceset="NMOS_Q1" device="" value="BSS138" />
        <part name="U1" library="circuit" deviceset="MCU_U1" device="" value="MCU" />
      </parts>
      <sheets>
        <sheet>
          <plain />
          <instances>
            <instance part="R3" gate="G$1" x="33.02" y="-38.1" />
            <instance part="C17" gate="G$1" x="104.14" y="10.16" />
            <instance part="D1" gate="G$1" x="121.92" y="-55.88" />
            <instance part="Q1" gate="G$1" x="43.18" y="-116.84" />
            <instance part="U1" gate="G$1" x="127" y="-124.46" />
          </instances>
          <busses />
          <nets />
        </sheet>
      </sheets>
    </schematic>
  </drawing>
</eagle>
