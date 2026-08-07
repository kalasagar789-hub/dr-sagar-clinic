export type Status="Booked"|"Checked In"|"Vitals Pending"|"Waiting"|"In Consultation"|"Completed"|"Cancelled"|"No Show"|"Payment Pending";
export type Mode="Clinic"|"Video Consultation"|"Audio Consultation"|"Home Visit";
export type Appointment={id:string;token:string;patient:string;uhid:string;age:number;gender:"Male"|"Female"|"Other";mobile:string;provider:string;department:string;time:string;mode:Mode;status:Status;payment:"Paid"|"Pending";priority:"Routine"|"Urgent"|"Emergency"};
