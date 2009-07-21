#!/usr/bin/env python
#
# Software License Agreement (BSD License)
#
# Copyright (c) 2008, Willow Garage, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of the Willow Garage nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

# Author: Kevin Watts

import roslib
roslib.load_manifest('pr2_computer_monitor')

import rospy

import traceback
import threading
from threading import Timer
import sys, os, time
import subprocess

import socket

from diagnostic_msgs.msg import * 

low_hd_level = 5
critical_hd_level = 1

hd_temp_warn = 50
hd_temp_error = 55

stat_dict = { 0: 'OK', 1: 'Warning', 2: 'Error' }
temp_dict = { 0: 'OK', 1: 'Warm', 2: 'Hot' }

## Deprecated. Use socket to hddtemp daemon instead
def get_hddtemp_data():
    hds = ['/dev/sda', '/dev/sdb']

    drives = []
    makes = []
    temps = []

    try:
        for hd in hds:
            p = subprocess.Popen('hddtemp %s' % hd,
                                 stdout = subprocess.PIPE,
                                 stderr = subprocess.PIPE, shell = True)
            stdout, stderr = p.communicate()
            retcode = p.returncode
            
            stdout = stdout.replace('\n', '')
            stderr = stderr.replace('\n', '')

            rospy.logerr('Hddtemp output: %s\n%s' % (stdout, stderr))

            lst = stdout.split(':')
            if len(lst) > 2:
                dev_id = lst[1]
                tmp = lst[2].strip()[:2] # Temp shows up as ' 40dC'
                
                if unicode(tmp).isnumeric():
                    temp = float(tmp)
                     
                    drives.append(hd)
                    makes.append(dev_id)
                    temps.append(temp)

                
    except:
        rospy.logerr(traceback.format_exc())
        
    return drives, makes, temps

## Connects to hddtemp daemon to get temp, HD make.
def get_hddtemp_data_socket(hostname = 'localhost', port = 7634):
    try:
        hd_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        hd_sock.connect((hostname, port))
        sock_data = ''
        while True:
            newdat = hd_sock.recv(1024)
            if len(newdat) == 0:
                break
            sock_data = sock_data + newdat
        hd_sock.close()
        
        sock_vals = sock_data.split('|')


        idx = 0
        
        drives = []
        makes = []
        temps = []
        while idx + 5 < len(sock_vals):
            drives.append(sock_vals[idx + 1])
            makes.append(sock_vals[idx + 2])
            temp = float(sock_vals[idx + 3])
            temps.append(temp)
                        
            idx = idx + 5

        return drives, makes, temps
    except:
        rospy.logerr(traceback.format_exc())
        return [], [], []

def update_status_stale(stat, last_update_time):
    time_since_update = rospy.get_time() - last_update_time

    level = stat.level
    stale_status = 'OK'
    if time_since_update > 20:
        stale_status = 'Lagging'
        level = max(level, 1)
    if time_since_update > 35:
        stale_status = 'Stale'
        level = max(level, 2)
        
    stat.strings.pop(0)
    stat.values.pop(0)
    stat.strings.insert(0, DiagnosticString(label = 'Update Status', value = stale_status))
    stat.values.insert(0, DiagnosticValue(label = 'Time Since Update', value = time_since_update))

class hdMonitor():
    def __init__(self, hostname, home_dir = ''):
        rospy.init_node('hd_monitor_%s' % hostname)

        self._mutex = threading.Lock()
        
        self._hostname = hostname
        self._no_temp_warn =  rospy.get_param('no_hd_temp_warn', False)
        self._home_dir = home_dir

        self._diag_pub = rospy.Publisher('/diagnostics', DiagnosticMessage)

        self._temp_stat = DiagnosticStatus()
        self._temp_stat.name = "%s HD Temperature" % hostname
        self._temp_stat.level = 2
        self._temp_stat.message = 'No Data'
        self._temp_stat.strings = [ DiagnosticString(label = 'Update Status', value = 'No Data' )]
        self._temp_stat.values = [ DiagnosticValue(label = 'Time Since Last Update', value = 100000 )]

        if self._home_dir != '':
            self._usage_stat = DiagnosticStatus()
            self._usage_stat.level = 2
            self._usage_stat.name = '%s HD Usage' % hostname
            self._usage_stat.strings = [ DiagnosticString(label = 'Update Status', value = 'No Data' )]
            self._usage_stat.values = [ DiagnosticValue(label = 'Time Since Last Update', value = 100000) ]
            self.check_disk_usage()

        self._last_temp_time = 0
        self._last_usage_time = 0
        self._last_publish_time = 0
        
        self.check_temps()

        self._temp_timer = None
        self._usage_timer = None
        self._publish_timer = threading.Timer(1.0, self.publish_stats)
        self._publish_timer.start()
        
        ## Must have the lock to cancel everything
    def cancel_timers(self):
        if self._temp_timer:
            self._temp_timer.cancel()
 
        if self._usage_timer:
            self._usage_timer.cancel()

        if self._publish_timer:
            self._publish_timer.cancel()

    def check_temps(self):
        if rospy.is_shutdown():
            self._mutex.acquire()
            self.cancel_timers()
            self._mutex.release()
            return

        diag_strs = [ DiagnosticString(label = 'Update Status', value = 'OK' ) ]
        diag_vals = [ DiagnosticValue(label = 'Time Since Last Update', value = 0 ) ]
        diag_level = 0
        
        
        drives, makes, temps = get_hddtemp_data_socket()
        if len(drives) == 0:
            diag_strs.append(DiagnosticString(label = 'Disk Temp Data', value = 'No hddtemp data'))
            diag_level = 2

        for index in range(0, len(drives)):
            temp = temps[index]
            temp_level = 0
            if temp > hd_temp_warn:
                temp_level = 1
            if temp > hd_temp_error:
                temp_level = 2
            diag_level = max(diag_level, temp_level)

            diag_strs.append(DiagnosticString(label = 'Disk %d Temp Status' % index, value = temp_dict[temp_level]))
            diag_strs.append(DiagnosticString(label = 'Disk %d Mount Pt.' % index, value = drives[index]))
            diag_strs.append(DiagnosticString(label = 'Disk %d Device ID' % index, value = makes[index]))
            diag_vals.append(DiagnosticValue(label = 'Disk %d Temp' % index, value = temp))
        
        self._mutex.acquire()
        self._last_temp_time = rospy.get_time()

        self._temp_stat.strings = diag_strs
        self._temp_stat.values = diag_vals

        self._temp_stat.level = diag_level

        # Give No Data message if we have no reading
        self._temp_stat.message = temp_dict[diag_level]
        if len(self._temp_stat.values) == 1: 
            self._temp_stat.message = 'No Data'

        if self._no_temp_warn and self._temp_stat.message != 'No Data':
            self._temp_stat.level = 0


        if not rospy.is_shutdown():
            self._temp_timer = threading.Timer(10.0, self.check_temps)
            self._temp_timer.start()
        else:
            self.cancel_timers()

        self._mutex.release()
        
    def check_disk_usage(self):
        diag_strs = [ DiagnosticString(label = 'Update Status', value = 'OK' ) ]
        diag_vals = [ DiagnosticValue(label = 'Time Since Last Update', value = 0 ) ]
        diag_level = 0
        
        try:
            p = subprocess.Popen(["df", "-P", "--block-size=1G", self._home_dir], 
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = p.communicate()
            retcode = p.returncode
            
            if (retcode == 0):
                
                diag_strs.append(DiagnosticString(label = 'Disk Space Reading', value = 'OK'))
                row_count = 0
                for row in stdout.split('\n'):
                    if len(row.split()) < 2:
                        continue
                    if not unicode(row.split()[1]).isnumeric() or float(row.split()[1]) < 10: # Ignore small drives
                        continue
                
                    row_count += 1
                    g_available = float(row.split()[-3])
                    name = row.split()[0]
                    size = float(row.split()[1])
                    mount_pt = row.split()[-1]
                    
                    if (g_available > low_hd_level):
                        level = 0
                    elif (g_available > critical_hd_level):
                        level = 1
                    else:
                        level = 2
                        
                    diag_strs.append(DiagnosticString(
                            label = 'Disk %d Name' % row_count, value = name))
                    diag_vals.append(DiagnosticString(
                            label = 'Disk %d Available' % row_count, value = g_available))
                    diag_vals.append(DiagnosticString(
                            label = 'Disk %d Size' % row_count, value = size))
                    diag_strs.append(DiagnosticString(
                            label = 'Disk %d Status' % row_count, value = stat_dict[level]))
                    diag_strs.append(DiagnosticString(
                            label = 'Disk %d Mount Point' % row_count, value = mount_pt))
                    
                    diag_level = max(diag_level, level)
                
            else:
                diag_strs.append(DiagnosticString(label = 'Disk Space Reading', value = 'Failed'))
                diag_level = 2
            
                    
        except:
            rospy.logerr(traceback.format_exc())
            diag_strs.append(DiagnosticString(label = 'Disk Space Reading', value = 'Exception'))
            diag_strs.append(DiagnosticString(label = 'Disk Space Ex', value = traceback.format_exc()))

            diag_level = 2
            
            stat.message = stat_dict[stat.level]

        # Update status
        self._mutex.acquire()
        self._last_usage_time = rospy.get_time()
        self._usage_stat.level = diag_level
        self._usage_stat.values = diag_vals
        self._usage_stat.strings = diag_strs
        
        self._usage_stat.message = stat_dict[diag_level]

        if not rospy.is_shutdown():
            self._usage_timer = threading.Timer(5.0, self.check_disk_usage)
            self._usage_timer.start()
        else:
            self.cancel_timers()

        self._mutex.release()

        
    def publish_stats(self):
        self._mutex.acquire()
        update_status_stale(self._temp_stat, self._last_temp_time)
        
        msg = DiagnosticMessage()
        msg.status.append(self._temp_stat)
        if self._home_dir != '':
            update_status_stale(self._usage_stat, self._last_usage_time)
            msg.status.append(self._usage_stat)
        
        if rospy.get_time() - self._last_publish_time > 0.5:
            self._diag_pub.publish(msg)
            self._last_publish_time = rospy.get_time()

        if not rospy.is_shutdown():
            self._publish_timer = threading.Timer(1.0, self.publish_stats)
            self._publish_timer.start()
        else:
            self.cancel_timers()

        self._mutex.release()


        
# TODO: Need to check HD input/output too using iostat

if __name__ == '__main__':
    hostname = socket.gethostname()

    home_dir = ''
    if len(rospy.myargv()) > 1:
        home_dir = rospy.myargv()[1]

    hd_monitor = hdMonitor(hostname, home_dir)
    

            
